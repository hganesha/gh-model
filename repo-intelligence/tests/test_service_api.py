"""The connect -> compile -> read path the Explorer uses, on a local fixture repository."""

import json
import time

import psycopg
import pytest
from fastapi.testclient import TestClient

from repo_intel import projection
from repo_intel.api import create_app
from repo_intel.github import GitHubError, parse_slug
from repo_intel.service import Service
from repo_intel.store import ObjectStore

TOKEN = "test-token-for-local-cluster-only"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path, pg_conninfo):
    with psycopg.connect(pg_conninfo) as admin:
        projection.ensure_schema(admin)
        projection.truncate_all(admin)
    app_conninfo = pg_conninfo.replace("user=rkp ", f"user={projection.APP_ROLE} ")
    def make(allow_local=True):
        store = ObjectStore(tmp_path / "objects", "tenant-a")
        return TestClient(create_app(app_conninfo, "tenant-a", TOKEN, store, Service(store, tmp_path / "objects", app_conninfo, allow_local_paths=allow_local)))
    return make


def _finish(c, job_id):
    for _ in range(200):
        job = c.get(f"/v1/compilations/{job_id}", headers=AUTH).json()
        if job["status"] in ("complete", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("compile did not finish")


def test_connect_compile_and_read_a_repository(client, f01):
    repo, shas = f01
    c = client()
    started = c.post("/v1/repositories", headers=AUTH, json={"local_path": str(repo), "ref": shas[0]})
    assert started.status_code == 200 and started.json()["commit_sha"] == shas[0] and "events" not in started.json()
    job = _finish(c, started.json()["id"])
    assert job["status"] == "complete" and job["rkp_hash"].startswith("sha256:")
    stages = [e["stage"] for e in job["events"]]
    assert stages[0] == "fetch" and stages[-1] == "complete" and {"snapshot", "extract", "link_and_compile", "published", "project"} <= set(stages)

    stream = c.get(f"/v1/compilations/{job['id']}/events", headers=AUTH).text
    assert stream.count("data: ") == len(job["events"]) + 1 and "event: done" in stream
    resumed = c.get(f"/v1/compilations/{job['id']}/events", headers=AUTH, params={"after": len(job["events"]) - 2}).text
    assert resumed.count("data: ") == 2, "only events after `after`, plus the final job"

    repos = c.get("/v1/repositories", headers=AUTH).json()
    assert [r["full_name"] for r in repos["repositories"]] == ["repo"] and repos["repositories"][0]["snapshots"][0]["rkp_hash"] == job["rkp_hash"]
    assert "local_path" in repos["repositories"][0] and set(repos["planes"]) == {"architecture", "contracts", "data", "ai", "tests", "deployment"}

    h = job["rkp_hash"]
    summary = c.get(f"/v1/snapshots/{h}/summary", headers=AUTH).json()
    assert summary["topology"] == "single" and summary["planes"]["contracts"]["kinds"]["Endpoint"] == 2 and summary["planes"]["data"]["kinds"]["Table"] == 1
    assert summary["coverage_totals"]["route_to_handler"] == {"numerator": 2, "denominator": 2} and summary["files"] == 9
    assert summary["coverage_totals"]["dependency_capability"] == {"numerator": 3, "denominator": 3}

    endpoints = c.get(f"/v1/snapshots/{h}/entities", headers=AUTH, params={"plane": "contracts"}).json()
    assert sorted(e["name"] for e in endpoints["entities"]) == ["GET /customers", "POST /customers"] and endpoints["truncated"] is False
    found = c.get(f"/v1/snapshots/{h}/entities", headers=AUTH, params={"q": "create_cust"}).json()["entities"]
    assert [e["name"] for e in found] == ["create_customer"]
    assert c.get(f"/v1/snapshots/{h}/entities", headers=AUTH, params={"q": "%"}).json()["entities"] == [], "wildcards in the search text are literal"
    assert c.get(f"/v1/snapshots/{h}/entities", headers=AUTH, params={"plane": "files", "limit": 3}).json()["truncated"] is True

    detail = c.get(f"/v1/snapshots/{h}/entities/{found[0]['id']}", headers=AUTH).json()
    predicates = {a["predicate"] for a in detail["assertions"]}
    assert {"IMPLEMENTS", "WRITES_TO", "DEFINED_IN", "TESTED_BY"} <= predicates
    assert all(a["subject"] in detail["neighbours"] and a["object"] in detail["neighbours"] for a in detail["assertions"])

    decisions = c.get(f"/v1/snapshots/{h}/decisions", headers=AUTH).json()["decisions"]
    assert {d["spec"].split("@")[0] for d in decisions} == {"dm.component-kind", "dm.repository-topology", "dm.dependency-capability"}
    deps = c.get(f"/v1/snapshots/{h}/entities", headers=AUTH, params={"kind": "ExternalDependency"}).json()["entities"]
    fastapi = next(e for e in deps if e["name"] == "fastapi")
    dependency = c.get(f"/v1/snapshots/{h}/entities/{fastapi['id']}", headers=AUTH).json()
    provides = next(a for a in dependency["assertions"] if a["predicate"] == "PROVIDES")
    assert dependency["neighbours"][provides["object"]]["kind"] == "TechnicalCapability"
    decision = next(d for d in decisions if d["decision_id"] == provides["decision_ref"])
    assert decision["decision_method"] == "rules" and decision["selected_label"] == "web-application-framework"
    assert decision["evidence_ids"] == provides["evidence_ids"]

    again = _finish(c, c.post("/v1/repositories/" + repos["repositories"][0]["repository_id"] + "/compilations", headers=AUTH, json={"ref": shas[1]}).json()["id"])
    assert again["status"] == "complete" and again["rkp_hash"] != h
    assert len(c.get("/v1/repositories", headers=AUTH).json()["repositories"][0]["snapshots"]) == 2


def test_service_fails_closed_and_reports_errors_as_text(client, f01, tmp_path):
    repo, shas = f01
    c = client()
    assert c.get("/v1/repositories").status_code == 401
    assert c.post("/v1/repositories", json={"local_path": str(repo)}).status_code == 401
    assert c.get("/v1/snapshots/sha256:" + "0" * 64 + "/summary", headers=AUTH).status_code == 404
    assert c.get("/v1/compilations/cmp_nope", headers=AUTH).status_code == 404
    assert c.post("/v1/repositories/github.com/999/compilations", headers=AUTH, json={}).status_code == 404
    not_git = c.post("/v1/repositories", headers=AUTH, json={"local_path": str(tmp_path)})
    assert not_git.status_code == 400 and not_git.json()["message"] == "not a Git repository"
    assert c.post("/v1/repositories", headers=AUTH, json={"url": "https://example.com/a/b"}).status_code == 400
    assert c.post("/v1/repositories", headers=AUTH, json={"url": "a/b", "token": "x"}).status_code == 422

    locked = client(allow_local=False)
    refused = locked.post("/v1/repositories", headers=AUTH, json={"local_path": str(repo)})
    assert refused.status_code == 400 and "disabled" in refused.json()["message"]


def test_repository_urls_are_parsed_strictly():
    assert parse_slug("https://github.com/fastapi/full-stack-fastapi-template") == ("fastapi", "full-stack-fastapi-template")
    assert parse_slug("git@github.com:owner/repo.git") == ("owner", "repo") and parse_slug(" owner/repo/ ") == ("owner", "repo")
    for bad in ["https://gitlab.com/a/b", "a/b/c", "a/..", "-x/y", "a/b; rm -rf /", "a/$(id)", "", "https://github.com/a"]:
        with pytest.raises(GitHubError):
            parse_slug(bad)


def test_hosted_database_applies_schema_and_connects_as_the_app_role(pg_conninfo):
    import psycopg
    import pytest
    from psycopg.conninfo import conninfo_to_dict
    from repo_intel import projection
    from repo_intel.serve import external_postgres

    app = external_postgres(pg_conninfo + " sslmode=disable", "a-long-app-password-123")
    params = conninfo_to_dict(app)
    assert params["user"] == projection.APP_ROLE and params["password"] == "a-long-app-password-123"
    assert params["sslmode"] == "disable" and params["host"] == "127.0.0.1", "the owner's host and options carry over"
    with psycopg.connect(app) as conn:
        assert conn.execute("SELECT current_user").fetchone()[0] == projection.APP_ROLE
        assert conn.execute("SELECT count(*) FROM business_maps").fetchone()[0] == 0, "no tenant setting: row-level security shows nothing"
    with pytest.raises(SystemExit):
        external_postgres(pg_conninfo, "short")
