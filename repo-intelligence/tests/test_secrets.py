import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import REPO_ID
from repo_intel import projection, secrets
from repo_intel.api import create_app
from repo_intel.contracts import entity_id
from repo_intel.fixtures import list_fixtures, load_golden, materialize
from repo_intel.hashing import canonical
from repo_intel.pipeline import PublishGateError, compile_commit, leaked_secrets, load_shards, stage_snapshot
from repo_intel.store import ObjectStore

TOKEN = "test-token-for-local-cluster-only"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PROBED = [f for f in list_fixtures() if load_golden(f).get("secret_probes")]


def test_scan_reports_location_not_value():
    data = b'aws = "AKIAIOSFODNN7EXAMPLE"\nurl = "postgres://app:hunter2pw@db/x"\napi_key: "abcdefghijklmnopqrstuvwxyz012345"\n'
    found = secrets.scan(data)
    assert [f["detector"] for f in found] == ["aws_access_key_id", "url_credentials", "generic_assignment"]
    assert all(set(f) == {"detector", "start_byte", "end_byte"} for f in found)
    assert data[found[1]["start_byte"]:found[1]["end_byte"]] == b"hunter2pw"
    text, masked = secrets.mask(data, 0, len(data), found)
    assert masked and b"AKIA" not in text and b"hunter2pw" not in text and b"abcdefghij" not in text
    assert b"[REDACTED:aws_access_key_id]" in text
    partial, _ = secrets.mask(data, found[0]["start_byte"] + 4, found[0]["end_byte"] - 2, found)  # a span cutting into a secret
    assert b"IOSFODNN" not in partial
    assert secrets.scan(b"DATABASE_URL=postgresql://localhost/orders\n") == []


@pytest.mark.parametrize("name", PROBED)
def test_planted_secret_never_reaches_compiler_output(name, tmp_path):
    shas = materialize(name, tmp_path / "repo")
    store = ObjectStore(tmp_path / "store", "tenant-a")
    for sha in shas:
        result = compile_commit(store, tmp_path / "repo", REPO_ID, sha)
        pm = store.get_json(stage_snapshot(store, tmp_path / "repo", sha))
        docs = [*load_shards(store, result.manifest).values(), store.get_json(result.manifest.coverage_ref), result.manifest.row(), pm]
        for probe in load_golden(name)["secret_probes"]:
            flagged = next(e for e in pm["entries"] if e["path"] == probe["path"])
            assert flagged["secret_findings"], "the planted secret must be detected"
            assert all(probe["must_never_appear"].encode() not in canonical(d) for d in docs)


def test_publish_gate_blocks_a_leaking_extractor(tmp_path, f01, monkeypatch):
    repo, shas = f01
    store = ObjectStore(tmp_path / "store", "tenant-a")
    pm = store.get_json(stage_snapshot(store, repo, shas[0]))
    assert leaked_secrets(store, pm, [{"oops": "key is AKIAIOSFODNN7EXAMPLE"}]) == [".env.example"]
    assert leaked_secrets(store, pm, [{"fine": "AWS_ACCESS_KEY_ID"}]) == []

    from repo_intel import pipeline
    monkeypatch.setattr(pipeline, "leaked_secrets", lambda *a: [".env.example"])
    with pytest.raises(PublishGateError, match="secret value"):
        compile_commit(store, repo, REPO_ID, shas[0])


def test_evidence_endpoint_masks_secrets_and_fails_closed(tmp_path, f01, pg_conninfo):
    repo, shas = f01
    store = ObjectStore(tmp_path / "store", "tenant-a")
    result = compile_commit(store, repo, REPO_ID, shas[0])
    with psycopg.connect(pg_conninfo) as conn:
        projection.ensure_schema(conn)
        projection.truncate_all(conn)
        projection.project_snapshot(conn, store, result.manifest)
    shards = load_shards(store, result.manifest)
    env_file = entity_id(REPO_ID, "Artifact", {"path": ".env.example"})
    ev = next(e for s in shards.values() for e in s["entities"] if e["id"] == env_file)["evidence_ids"][0]
    client = TestClient(create_app(pg_conninfo, "tenant-a", TOKEN, store))
    params = {"rkp_hash": result.manifest.rkp_hash}

    body = client.get(f"/v1/evidence/{ev}", params=params, headers=AUTH).json()
    assert body["status"] == "ok" and body["masked"] is True and body["paths"] == [".env.example"]
    assert "AKIAIOSFODNN7EXAMPLE" not in body["text"] and "[REDACTED:aws_access_key_id]" in body["text"]
    assert "DATABASE_URL=postgresql://localhost/orders" in body["text"] and body["trust_label"] == "untrusted_data"

    assert client.get(f"/v1/evidence/{ev}", params=params).status_code == 401
    assert client.get("/v1/evidence/ev_doesnotexist", params=params, headers=AUTH).status_code == 404
    assert TestClient(create_app(pg_conninfo, "tenant-b", TOKEN, ObjectStore(tmp_path / "store", "tenant-b"))).get(f"/v1/evidence/{ev}", params=params, headers=AUTH).status_code == 404
    assert TestClient(create_app(pg_conninfo, "tenant-a", TOKEN)).get(f"/v1/evidence/{ev}", params=params, headers=AUTH).status_code == 404


def test_expressions_are_not_secrets_but_env_lines_are():
    code = b"password_reset_token = generate_password_reset_token(email=email)\nsend(token=password_reset_token)\n"
    assert secrets.scan(code) == [], "an identifier on the right-hand side is an expression, not a value"
    env = b"DEBUG=true\nSECRET_KEY=0123456789abcdefghij0123\nSMTP_PASSWORD=${SMTP_PASSWORD}\nFIRST_SUPERUSER_PASSWORD=changethis\n"
    found = secrets.scan(env)
    assert [f["detector"] for f in found] == ["env_assignment"] and env[found[0]["start_byte"]:found[0]["end_byte"]] == b"0123456789abcdefghij0123"
