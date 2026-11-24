import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import REPO_ID
from repo_intel.knowledge import projection
from repo_intel.ingest import secrets
from repo_intel.server.api import create_app
from repo_intel.compile.contracts import Entity, Evidence, entity_id
from repo_intel.extractors.base import whole_file
from repo_intel.compile.fixtures import list_fixtures, load_golden, materialize
from repo_intel.storage.hashing import canonical
from repo_intel.compile.pipeline import PublishGateError, compile_commit, leaked_secrets, load_shards, stage_snapshot
from repo_intel.storage.store import ObjectStore

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

    from repo_intel.compile import pipeline
    monkeypatch.setattr(pipeline, "leaked_secrets", lambda *a: [".env.example"])
    with pytest.raises(PublishGateError, match="secret value"):
        compile_commit(store, repo, REPO_ID, shas[0])


def test_url_password_collision_requires_independent_cited_identity(store):
    source = b"DATABASE_URL=postgres://app:catalog@db/work\n"
    code = b"def catalog(): pass\n"
    entries = [
        {"path": path, "blob_sha256": store.put_bytes(data).split(":", 1)[1], "secret_findings": secrets.scan(data)}
        for path, data in (("config.env", source), ("catalog.py", code))
    ]
    cited = [Evidence.make(entry["blob_sha256"], whole_file(data), "symbol").row() for entry, data in zip(entries, (source, code))]

    def shard(ref):
        row = Entity.make(REPO_ID, "Symbol", {"qualified_name": "catalog.catalog"}, "catalog", evidence_ids=[ref]).row()
        return {"unit": ".", "entities": [row], "assertions": [], "unknowns": [], "evidence": cited}

    manifest = {"entries": entries}
    assert leaked_secrets(store, manifest, [shard(cited[1]["id"])]) == []
    assert leaked_secrets(store, manifest, [shard(cited[0]["id"])]) == ["config.env"]
    assert leaked_secrets(store, manifest, [{"attributes": {"password": "prefix-catalog-suffix"}}]) == ["config.env"]
    arbitrary_conflict = {
        "conflict_type": "identity_ambiguity", "assertion_ids": [],
        "evidence_ids": [cited[1]["id"]], "basis": "catalog",
    }
    assert leaked_secrets(store, manifest, [[arbitrary_conflict], shard(cited[1]["id"])]) == ["config.env"]
    assert leaked_secrets(store, manifest, [{"https://app:catalog@db/work": True}]) == ["config.env"]
    assert leaked_secrets(store, {"entries": []}, [{"connection": "https://app:another@db/work"}]) == ["<published output>"]


def test_redacted_extraction_preserves_offsets_and_keeps_raw_credentials_from_parsers(store):
    from repo_intel.compile.buildgraph import StepRunner
    from repo_intel.extractors import extract_file

    source = b'''from fastapi import FastAPI
dsn = "postgres://app:fixture-password@db/work"
api = FastAPI()
@api.get("/catalog")
def catalog(): pass
'''
    findings = secrets.scan(source)
    assert len(findings) == 1
    clean = secrets.mask_for_extraction(source, findings)
    assert len(clean) == len(source) and b"fixture-password" not in clean
    runner = StepRunner(store)
    entry = {"path": "app.py", "blob_sha256": store.put_bytes(source).split(":", 1)[1], "parse": True, "secret_findings": findings}
    parsed = runner.load(extract_file(runner, entry))
    route = next(o for o in parsed["observations"] if o["kind"] == "route_handler")
    span = route["evidence"]["span"]
    assert source[span["start_byte"]:span["end_byte"]].startswith(b'@api.get("/catalog")')
    assert route["evidence"]["blob_sha256"] == entry["blob_sha256"]


def test_publish_gate_still_detects_a_multiline_private_key(store):
    fixture = b"-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----"
    entry = {
        "path": "fixture.pem", "blob_sha256": store.put_bytes(fixture).split(":", 1)[1],
        "secret_findings": secrets.scan(fixture),
    }
    assert leaked_secrets(store, {"entries": [entry]}, [{"output": fixture.decode()}]) == ["fixture.pem"]


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
