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


def _flagged(data: bytes) -> list[tuple[str, bytes]]:
    return [(f["detector"], data[f["start_byte"]:f["end_byte"]]) for f in secrets.scan(data)]


def test_servlet_param_values_named_as_secrets_are_found():
    web_xml = b"""<context-param>
    <param-name>mail.password</param-name>
    <param-value>fixture-not-a-real-password</param-value>
  </context-param>
  <init-param><param-name>db.PWD</param-name><!-- set at deploy --><param-value> S3cret!pass </param-value></init-param>
  <init-param><param-name>clientSecret</param-name><param-value>abcdefghijklmnopqrstu</param-value></init-param>
  <init-param><param-name>apiKey</param-name><param-value>k-123456789</param-value></init-param>
"""
    assert _flagged(web_xml) == [("servlet_param_value", b"fixture-not-a-real-password"), ("servlet_param_value", b"S3cret!pass"),
                                 ("servlet_param_value", b"abcdefghijklmnopqrstu"), ("servlet_param_value", b"k-123456789")]
    # names that only mention the word, placeholders, class names, paths and short words are not secrets
    not_secrets = b"""<init-param><param-name>passwordParameter</param-name><param-value>j_password</param-value></init-param>
  <init-param><param-name>keyGenerator</param-name><param-value>com.acme.KeyGen</param-value></init-param>
  <init-param><param-name>apiKey</param-name><param-value>${API_KEY}</param-value></init-param>
  <init-param><param-name>signingKey</param-name><param-value>/WEB-INF/keys/signing.jks</param-value></init-param>
  <init-param><param-name>keystorePassword</param-name><param-value>changeit</param-value></init-param>
  <init-param><param-name>encodingKey</param-name><param-value>true</param-value></init-param>
  <init-param><param-name>encoding</param-name><param-value>fixture-not-a-real-password</param-value></init-param>
"""
    assert secrets.scan(not_secrets) == []


def test_connection_string_passwords_are_found():
    config = b"""<connectionStrings>
    <add name="DeskDb" connectionString="Server=db.internal;Database=Desk;User Id=desk;Password=fixture-not-a-real-password;" />
    <add name="Odbc" connectionString="Driver={SQL Server};Uid=u;PWD = Tr0ub4dor&amp;3" />
  </connectionStrings>
  <appSettings><add key="Reports" value="Data Source=r;Pwd=r3ports-2024;" /></appSettings>
"""
    assert _flagged(config) == [("connection_string_password", b"fixture-not-a-real-password"), ("connection_string_password", b"Tr0ub4dor&amp;3"),
                                ("connection_string_password", b"r3ports-2024")]
    assert _flagged(b'{"ConnectionStrings": {"Default": "Server=.;User Id=sa;Password=P@ssw0rd123"}}') == [("connection_string_password", b"P@ssw0rd123")]
    # a short or placeholder password, a password built in code, and a password input are not flagged
    for text in (b'connectionString="Server=x;Password=desk;"', b'connectionString="Server=x;Password={0};"', b'connectionString="Server=x;Password=%DB_PASSWORD%"',
                 b'var s = "Password=" + pwd;', b'<input type="password" value="hunter2hunter2" />', b"https://x/login?user=a&password=s3cret-value"):
        assert secrets.scan(text) == [], text


def test_masked_connection_string_keeps_offsets_and_names():
    config = b'<add name="DeskDb" connectionString="Server=db;Password=fixture-not-a-real-password;" providerName="System.Data.SqlClient" />'
    clean = secrets.mask_for_extraction(config, secrets.scan(config))
    assert len(clean) == len(config) and b"fixture-not-a-real-password" not in clean
    assert b'name="DeskDb"' in clean and b"Password=" in clean and b'providerName="System.Data.SqlClient"' in clean


def test_expressions_are_not_secrets_but_env_lines_are():
    code = b"password_reset_token = generate_password_reset_token(email=email)\nsend(token=password_reset_token)\n"
    assert secrets.scan(code) == [], "an identifier on the right-hand side is an expression, not a value"
    env = b"DEBUG=true\nSECRET_KEY=0123456789abcdefghij0123\nSMTP_PASSWORD=${SMTP_PASSWORD}\nFIRST_SUPERUSER_PASSWORD=changethis\n"
    found = secrets.scan(env)
    assert [f["detector"] for f in found] == ["env_assignment"] and env[found[0]["start_byte"]:found[0]["end_byte"]] == b"0123456789abcdefghij0123"


def test_mainframe_credentials_are_found_but_references_and_short_words_are_not():
    cobol = (b"       01  WS-DB-PASSWD PIC X(30) VALUE 'fixture-cobol-value-01'.\n"
             b"           MOVE 'fixture-cobol-move-02' TO WS-DB-PASSWD.\n"
             b"           MOVE 'not-a-secret-but-long' TO WS-CUSTOMER-NAME.\n"
             b"           MOVE 'short' TO WS-PWD.\n")
    assert _flagged(cobol) == [("cobol_value_credential", b"fixture-cobol-value-01"), ("cobol_move_credential", b"fixture-cobol-move-02")]
    pli = b" DCL DB_PASSWORD CHAR(30) INIT('fixture-pli-init-03');\n DCL CUST_NAME CHAR(30) INIT('Customer-Name-Default');\n"
    assert _flagged(pli) == [("pli_init_credential", b"fixture-pli-init-03")]
    cl = b"SBMJOB CMD(CALL PGM(X)) USER(BATCHUSR)\nRUNRMTCMD CMD('x') RMTPWD('fixture-cl-pwd-04')\nRUNRMTCMD CMD('x') RMTPWD(&PWD)\n"
    assert _flagged(cl) == [("cl_password_keyword", b"fixture-cl-pwd-04")]


def test_url_placeholders_and_secret_names_are_not_findings(store):
    # A product default, the user name again or a reference to another value is no secret;
    # treating `postgres` as one failed the publish gate wherever the word named the driver.
    source = (
        b"DATABASE_URL=postgres://postgres:postgres@db:5432/app\n"
        b"QUEUE_URL=amqp://app:guest@mq\n"
        b"CACHE_URL=redis://app:${REDIS_PASSWORD}@cache\n"
        b"OTHER_URL=postgres://app:hunter2@db/x\n"
    )
    assert [f["detector"] for f in secrets.scan(source)] == ["url_credentials"]
    assert secrets.values(source, secrets.scan(source)) == [b"hunter2"]
    # A setting mapped to the Key Vault secret of its own name; a real value is still found.
    names = b'const SECRETS = { DATABASE_MIGRATOR_PASSWORD: "database-migrator-password" };\nDB_TOKEN=db-token-for-the-local-stack\n'
    assert [f["detector"] for f in secrets.scan(names)] == ["env_assignment"]
    assert secrets.scan(b'MODEL_PROVIDER_TOKEN="model-provider-token"\n') == []
    assert len(secrets.scan(b'MODEL_PROVIDER_TOKEN="model-provider-token-1234"\n')) == 1
    entry = {"path": "compose.yaml", "blob_sha256": store.put_bytes(source).split(":", 1)[1], "secret_findings": secrets.scan(source)}
    published = [{"name": "postgres", "url": "postgres://postgres:postgres@db:5432/app", "kv": "database-migrator-password"}]
    assert leaked_secrets(store, {"entries": [entry]}, published) == []
    assert leaked_secrets(store, {"entries": [entry]}, [{"url": "postgres://app:hunter2@db/x"}]) == ["compose.yaml"]
