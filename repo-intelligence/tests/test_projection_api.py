import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import REPO_ID
from repo_intel.knowledge import projection
from repo_intel.server.api import create_app
from repo_intel.compile.contracts import entity_id
from repo_intel.compile.pipeline import compile_commit

TOKEN = "test-token-for-local-cluster-only"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def projected(store, f01, pg_conninfo):
    repo, shas = f01
    results = [compile_commit(store, repo, REPO_ID, s) for s in shas[:2]]
    with psycopg.connect(pg_conninfo) as conn:
        projection.ensure_schema(conn)
        projection.truncate_all(conn)
        for r in results:
            projection.project_snapshot(conn, store, r.manifest)
    return store, results


def test_projection_rebuilds_identically_and_shares_shards(projected, pg_conninfo):
    store, results = projected
    with psycopg.connect(pg_conninfo) as conn:
        before = projection.dump(conn)
        assert len(before["snapshots"]) == 2
        distinct = {s.digest for r in results for s in r.manifest.shards}
        assert len(before["shards"]) == len(distinct)
        projection.truncate_all(conn)
        for r in reversed(results):
            projection.project_snapshot(conn, store, r.manifest)
            projection.project_snapshot(conn, store, r.manifest)  # idempotent
        assert projection.dump(conn) == before


def test_query_endpoint_returns_evidence_backed_subgraph(projected, pg_conninfo, f01):
    _, results = projected
    client = TestClient(create_app(pg_conninfo, "tenant-a", TOKEN))
    m = results[0].manifest

    got = client.get(f"/v1/repositories/{REPO_ID}/rkp", params={"commit_sha": f01[1][0]}, headers=AUTH)
    assert got.status_code == 200 and got.json()["rkp_hash"] == m.rkp_hash

    r = client.post("/v1/knowledge/query", headers=AUTH, json={"rkp_hash": m.rkp_hash, "seed_selector": {"kind": "Package", "name": "orders-api"}, "allowed_edge_types": ["DEPENDS_ON"], "max_depth": 1})
    assert r.status_code == 200
    body = r.json()
    names = {e["name"] for e in body["entities"]}
    assert names == {"orders-api", "fastapi", "sqlalchemy", "httpx"} and body["truncated"] is False
    known_evidence = {v["id"] for v in body["evidence"]}
    assert body["assertions"] and all(set(a["evidence_ids"]) <= known_evidence for a in body["assertions"])

    pkg = entity_id(REPO_ID, "Package", {"ecosystem": "pypi", "name": "orders-api", "manifest_dir": ""})
    small = client.post("/v1/knowledge/query", headers=AUTH, json={"rkp_hash": m.rkp_hash, "seed_entities": [pkg], "max_depth": 1, "max_nodes": 3}).json()
    again = client.post("/v1/knowledge/query", headers=AUTH, json={"rkp_hash": m.rkp_hash, "seed_entities": [pkg], "max_depth": 1, "max_nodes": 3}).json()
    assert small["truncated"] is True and len(small["entities"]) == 3 and small == again


def test_api_fails_closed(projected, pg_conninfo):
    _, results = projected
    q = {"rkp_hash": results[0].manifest.rkp_hash, "seed_selector": {"kind": "Package"}}
    client = TestClient(create_app(pg_conninfo, "tenant-a", TOKEN))
    assert client.post("/v1/knowledge/query", json=q).status_code == 401
    assert client.post("/v1/knowledge/query", json=q, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert TestClient(create_app(pg_conninfo, "tenant-a", None)).post("/v1/knowledge/query", json=q, headers=AUTH).status_code == 401
    other_tenant = TestClient(create_app(pg_conninfo, "tenant-b", TOKEN))
    missing = other_tenant.post("/v1/knowledge/query", json=q, headers=AUTH)
    unknown = client.post("/v1/knowledge/query", json={**q, "rkp_hash": "sha256:" + "0" * 64}, headers=AUTH)
    assert missing.status_code == unknown.status_code == 404 and missing.json() == unknown.json()
    assert client.post("/v1/knowledge/query", json={**q, "actor_ref": "admin"}, headers=AUTH).status_code == 422
