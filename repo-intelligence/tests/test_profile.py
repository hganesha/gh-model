"""The repository profile (repo_intel.knowledge.profile): a rollup of a snapshot's facts into labelled fields."""

import json

import pytest

from repo_intel.compile.fixtures import materialize
from repo_intel.knowledge import bundle, local_api, profile
from repo_intel.server.service import PLANES


def entity(kind, name, attributes=None, natural_key=None):
    return {"id": f"ent_{kind}_{name}", "kind": kind, "name": name, "attributes": attributes, "natural_key": natural_key or {}}


def component(name, kind, language="python", frameworks=(), status="supported", ecosystem="pypi"):
    return entity("Component", name, {"component_kind": kind, "primary_language": language, "frameworks": list(frameworks), "kind_status": status, "ecosystem": ecosystem})


def fields(rows=(), counts=None, coverage=()):
    return {f["id"]: f for f in profile.build(rows, counts or {}, coverage)["fields"]}


def texts(field):
    return [v["text"] for v in field["values"]]


def summary_of(tmp_path, fixture):
    path = tmp_path / fixture
    materialize(fixture, path)
    document = bundle.compile_bundle(path)
    status, body = local_api.LocalApi(path).handle("GET", f"snapshots/{document['rkp_hash']}/summary")
    assert status == 200
    return body


def test_every_field_is_present_and_an_empty_snapshot_finds_nothing():
    built = profile.build([], {}, [])
    assert [f["id"] for f in built["fields"]] == [f[0] for f in profile.FIELDS]
    assert {f["status"] for f in built["fields"]} == {"not_found"} and all(f["values"] == [] for f in built["fields"])
    assert {f["group"] for f in built["fields"]} == set(built["groups"]) and built["partial"] is False


def test_fields_open_planes_the_explorer_has():
    assert {plane for *_, plane in profile.FIELDS} <= {*PLANES, "files", None}


def test_a_front_end_with_a_service_is_a_web_application():
    f = fields([component("web", "frontend_app", "typescript", ["react-dom", "react"]), component("api", "service", frameworks=["fastapi"]), component("shared", "library")])
    assert texts(f["application_type"]) == ["Web application"] and f["application_type"]["status"] == "inferred"
    assert f["application_type"]["values"][0]["entity_ids"] == ["ent_Component_api", "ent_Component_web"]
    assert texts(f["front_end"]) == ["TypeScript · React"] and f["front_end"]["status"] == "supported"
    assert texts(f["back_end"]) == ["Python · FastAPI"] and f["back_end"]["values"][0]["detail"] == "1 back-end service"


def test_a_library_is_the_application_only_when_nothing_else_is():
    assert texts(fields([component("core", "library"), component("cli", "cli")])["application_type"]) == ["Command-line tool"]
    alone = fields([component("core", "library")])
    assert texts(alone["application_type"]) == ["Library"] and texts(alone["back_end"]) == ["Python"]


def test_a_component_kind_no_decision_settled_makes_the_stack_inferred():
    f = fields([component("api", "service", status="inferred"), component("jobs", "worker")])
    assert f["back_end"]["status"] == "inferred" and texts(f["application_type"]) == ["Back-end service", "Background worker"]


def test_the_ecosystem_stands_in_for_an_undecided_language_and_frameworks_get_their_names():
    row = component("catalog", "service", "unknown", ["org.springframework.boot:spring-boot-starter-web", "com.acme:in-house"], ecosystem="maven")
    assert texts(fields([row])["back_end"]) == ["Java or Kotlin · Spring Boot · in-house"]
    assert profile.framework_name("google.golang.org/grpc") == "gRPC" and profile.framework_name("@acme/ui") == "@acme/ui"


def test_languages_count_source_files_and_leave_out_manifests_and_configuration():
    coverage = [
        {"domain": "parse", "scope": "a", "detail": {"by_language": {"python": {"files": 3, "parsed": 3}, "json": {"files": 9, "parsed": 9}, "npm-manifest": {"files": 1, "parsed": 1}}}},
        {"domain": "parse", "scope": "b", "detail": {"by_language": {"typescript": {"files": 1, "parsed": 0}}}},
        {"domain": "behavior", "scope": "a", "detail": {"by_language": {"cobol": 40}}},
    ]
    languages = fields(coverage=coverage)["languages"]
    assert [(v["text"], v["detail"]) for v in languages["values"]] == [("Python", "75% of source files"), ("TypeScript", "25% of source files")]


def test_a_repository_with_only_a_legacy_platform_is_a_legacy_application():
    coverage = [{"domain": "platform_constructs", "scope": "cfml", "numerator": 5, "denominator": 5, "detail": {}}]
    value = fields(coverage=coverage)["application_type"]["values"][0]
    assert value["text"] == "Legacy application" and value["detail"] == "ColdFusion (CFML)" and value["entity_ids"] == []


def test_cloud_runtime_pipelines_and_messaging_are_grouped_by_what_they_are():
    rows = [
        entity("InfrastructureResource", "db", {"resource_provider": "aws"}, {"provider": "terraform", "type": "aws_db_instance"}),
        entity("InfrastructureResource", "vpc", {"resource_provider": "aws"}, {"provider": "terraform", "type": "aws_vpc"}),
        entity("InfrastructureResource", "bus", {"resource_provider": "microsoft"}, {"provider": "bicep"}),
        entity("DeploymentUnit", "api", {}, {"kind": "k8s.Deployment"}), entity("DeploymentUnit", "send", {}, {"kind": "aws.Lambda"}),
        entity("Pipeline", "deploy", {}, {"path": ".github/workflows/deploy.yml"}), entity("Pipeline", "nightly", {}, {"path": "ci/nightly.groovy"}),
        entity("EventTopic", "orders", {"transport": "kafka"}), entity("EventTopic", "jobs", {"transport": "sqs"}), entity("EventTopic", "audit", {"transport": "kafka"}),
        entity("Environment", "prod"), entity("Environment", "dev"), entity("Owner", "@acme/payments"),
    ]
    f = fields(rows)
    assert [(v["text"], v["detail"]) for v in f["cloud"]["values"]] == [("AWS", "2 resources declared with Terraform"), ("Azure", "1 resource declared with Bicep")]
    assert texts(f["runtime"]) == ["Kubernetes", "Serverless functions"] and texts(f["ci_cd"]) == ["GitHub Actions", "Pipeline"]
    assert [(v["text"], v["entities"]) for v in f["messaging"]["values"]] == [("Kafka", 2), ("Amazon SQS", 1)]
    assert texts(f["environments"]) == ["dev", "prod"] and texts(f["owners"]) == ["@acme/payments"]


def test_architecture_leads_with_granularity_then_styles_and_counts_the_rest():
    rows = [
        entity("ArchitecturePattern", "Saga", {"family": "Cross-boundary transactions", "kind": "architectural_pattern"}),
        entity("ArchitecturePattern", "Hexagonal architecture", {"family": "Dependency structure", "kind": "style"}),
        entity("ArchitecturePattern", "Microservices architecture", {"family": "Deployment granularity", "kind": "style"}),
    ]
    f = fields(rows)["architecture"]
    assert texts(f) == ["Microservices architecture", "Hexagonal architecture", "1 other pattern"] and f["status"] == "inferred"
    assert f["values"][2]["detail"] == "Saga"


def test_ai_names_the_provider_of_each_model_and_an_sdk_with_no_model():
    rows = [
        entity("ModelEndpoint", "claude-sonnet-5-5", {"basis": "identifier_pattern"}, {"provider": "anthropic", "model": "claude-sonnet-5-5"}),
        entity("AIComponent", "anthropic client", {"sdk": "anthropic", "call_sites": 2}), entity("AIComponent", "openai client", {"sdk": "openai", "call_sites": 1}),
    ]
    f = fields(rows)["ai"]
    assert [(v["text"], v["detail"]) for v in f["values"]] == [("Anthropic", "claude-sonnet-5-5"), ("OpenAI", "1 call site")] and f["status"] == "inferred"


def test_counts_fill_api_data_and_tests_without_reading_their_rows():
    f = fields([entity("Interface", "Orders", {"spec": "grpc"})], {"Endpoint": 1200, "Table": 1, "Migration": 4, "Test": 58})
    assert texts(f["api"]) == ["1,200 endpoints", "gRPC"] and texts(f["data"]) == ["1 table", "4 migrations"] and texts(f["tests"]) == ["58 tests"]


def test_long_fields_are_cut_and_say_how_much_is_left_out():
    rows = [entity("Environment", f"env-{i:02}") for i in range(profile.MAX_VALUES + 3)]
    f = fields(rows)["environments"]
    assert len(f["values"]) == profile.MAX_VALUES and f["more"] == 3
    many = fields([entity("EventTopic", f"t{i:03}", {"transport": "kafka"}) for i in range(profile.MAX_ENTITY_IDS + 5)])["messaging"]["values"][0]
    assert len(many["entity_ids"]) == profile.MAX_ENTITY_IDS and many["entities"] == profile.MAX_ENTITY_IDS + 5


def test_reading_past_the_row_limit_marks_the_profile_partial(monkeypatch):
    monkeypatch.setattr(profile, "ROW_LIMIT", 2)
    asked = []

    def list_entities(kinds, text, limit):
        asked.append((kinds, text, limit))
        return [entity("Environment", name) for name in ("a", "b", "c")][:limit]

    built = profile.for_snapshot(list_entities, {}, [])
    assert asked == [(profile.PROFILE_KINDS, None, 3)] and built["partial"] is True
    assert texts(next(f for f in built["fields"] if f["id"] == "environments")) == ["a", "b"]


def test_the_summary_of_a_hybrid_repository_carries_its_profile(tmp_path):
    summary = summary_of(tmp_path, "F03-python-ts-hybrid")
    f = {x["id"]: x for x in summary["profile"]["fields"]}
    assert texts(f["application_type"]) == ["Web application"] and texts(f["front_end"]) == ["TypeScript · React"] and texts(f["back_end"]) == ["Python · FastAPI"]
    assert texts(f["runtime"]) == ["Docker Compose"] and f["cloud"]["status"] == "not_found"
    known = {e["id"] for e in local_api.LocalApi(tmp_path / "F03-python-ts-hybrid").handle("GET", f"snapshots/{summary['manifest']['rkp_hash']}/entities?kind=Component")[1]["entities"]}
    assert set(f["application_type"]["values"][0]["entity_ids"]) == known
    json.dumps(summary["profile"])


@pytest.mark.parametrize("fixture, expected", [
    ("F06-iac-heavy", {"application_type": ["Back-end service"], "cloud": ["AWS"], "ci_cd": ["GitHub Actions"], "environments": ["prod", "staging"], "runtime": ["Docker Compose", "Kubernetes"]}),
    ("F20-coldfusion-app", {"application_type": ["Legacy application"], "languages": ["ColdFusion (CFML)"], "front_end": []}),
    ("F12-dbt-airflow", {"application_type": ["Data pipelines"], "back_end": ["Python · Airflow · dbt (Snowflake)", "SQL (dbt)"]}),
])
def test_profiles_of_fixture_repositories(tmp_path, fixture, expected):
    f = {x["id"]: x for x in summary_of(tmp_path, fixture)["profile"]["fields"]}
    assert {field_id: texts(f[field_id]) for field_id in expected} == expected
