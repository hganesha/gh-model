"""The Explorer's API over a bundle, without PostgreSQL (repo_intel.knowledge.local_api, `rkp serve --stdio`)."""

import io
import json
import shutil

import psycopg
import pytest

from repo_intel import cli
from repo_intel.compile.contracts import Manifest
from repo_intel.compile.fixtures import materialize
from repo_intel.knowledge import bundle, local_api, projection
from repo_intel.server.service import PLANES


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    materialize("F01-python-api", path)
    bundle.compile_bundle(path)
    return path


def _serve(repo, requests):
    stdin = io.StringIO("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in requests))
    stdout = io.StringIO()
    assert local_api.serve_stdio(repo, stdin=stdin, stdout=stdout) == 0
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_planes_match_the_server():
    assert local_api.PLANES == PLANES


@pytest.mark.parametrize("fixture", ["F01-python-api", "F02-ts-monorepo", "F07-event-driven", "F16-polyglot-contracts"])
def test_answers_match_the_postgres_projection(tmp_path, pg_conninfo, fixture):
    path = tmp_path / "repo"
    materialize(fixture, path)
    document = bundle.compile_bundle(path)
    store = bundle.store_for(path)
    manifest = Manifest.model_validate(document["snapshot"])
    with psycopg.connect(pg_conninfo) as conn:
        projection.ensure_schema(conn)
        projection.truncate_all(conn)
        projection.project_snapshot(conn, store, manifest)
        conn.commit()
    snap = local_api.Snapshot(store, manifest)
    t, h = store.tenant_id, manifest.rkp_hash
    with projection.connect(pg_conninfo, t) as conn:
        assert snap.count_by_kind() == projection.count_by_kind(conn, t, h)
        for kinds, text in [(None, None), (["Component", "Package"], None), (None, "app"), (["Artifact"], "py"), (["Endpoint"], "_%")]:
            assert snap.list_entities(kinds, text, 500) == projection.list_entities(conn, t, h, kinds, text, 500), (kinds, text)
        assert snap.find_entities("Package", None) == projection.find_entities(conn, t, h, "Package", None)
        ids = sorted(snap.entity_rows)
        for entity_id in ids[:: max(1, len(ids) // 25)]:
            assert snap.entity_detail(entity_id) == projection.entity_detail(conn, t, h, entity_id), entity_id
        seeds = ids[:3]
        for edges, depth, nodes in [(None, 2, 200), (["DEPENDS_ON", "CONTAINS"], 3, 50), (None, 1, 4), (None, 0, 10)]:
            assert snap.query_graph(seeds, edges, depth, nodes) == projection.query_graph(conn, t, h, seeds, edges, depth, nodes)


def test_stdio_serves_the_explorer_routes(repo):
    h = bundle.read_manifest(repo)["rkp_hash"]
    responses = _serve(repo, [
        {"id": 1, "method": "GET", "path": "local"},
        {"id": 2, "method": "GET", "path": f"snapshots/{h}/summary"},
        {"id": 3, "method": "GET", "path": f"snapshots/{h}/entities?plane=architecture&kind=Package&limit=5"},
        {"id": 4, "method": "GET", "path": f"snapshots/{h}/decisions"},
        {"id": 5, "method": "GET", "path": f"snapshots/{h}/dependencies"},
        {"id": 6, "method": "GET", "path": f"snapshots/{h}/placements"},
        {"id": 7, "method": "GET", "path": f"snapshots/{h}/exclusions"},
        {"id": 8, "method": "GET", "path": "snapshots/sha256:0000/summary"},
        {"id": 9, "method": "GET", "path": "entities/ent_x/lineage"},
        {"id": 10, "method": "POST", "path": "business-maps/placements", "body": {}},
        {"id": 11, "method": "GET", "path": "no/such/route"},
        "{not json",
        {"id": 13, "method": "GET", "path": f"snapshots/{h}/entities?plane=nope"},
    ])
    by_id = {r["id"]: r for r in responses}
    assert [r["id"] for r in responses] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, None, 13]
    assert by_id[1]["body"]["rkp_hash"] == h and by_id[1]["body"]["working_tree"] is False
    summary = by_id[2]["body"]
    assert summary["manifest"]["status"] == "complete" and summary["planes"]["architecture"]["total"] > 0
    packages = by_id[3]["body"]["entities"]
    assert packages and {e["kind"] for e in packages} == {"Package"}
    assert isinstance(by_id[4]["body"]["decisions"], list)
    assert "groups" in by_id[5]["body"] and by_id[6]["body"] == {"components": {}}
    assert by_id[7]["body"]["available"] is True
    assert by_id[8]["status"] == 404 and by_id[8]["body"]["code"] == "replaced"
    assert by_id[9]["status"] == 404 and by_id[10]["status"] == 501 and by_id[11]["status"] == 404
    assert responses[11]["status"] == 400
    assert by_id[13]["status"] == 404


def test_query_detail_and_evidence(repo):
    h = bundle.read_manifest(repo)["rkp_hash"]
    api = local_api.LocalApi(repo)
    status, body = api.handle("POST", "knowledge/query", {"rkp_hash": h, "seed_selector": {"kind": "Package", "name": "orders-api"}, "allowed_edge_types": ["DEPENDS_ON"], "max_depth": 1})
    assert status == 200 and {"orders-api", "fastapi", "sqlalchemy", "httpx"} <= {e["name"] for e in body["entities"]}
    package = next(e for e in body["entities"] if e["name"] == "orders-api")
    status, detail = api.handle("GET", f"snapshots/{h}/entities/{package['id']}")
    assert status == 200 and detail["entity"]["id"] == package["id"] and detail["assertions"]
    evidence_id = body["evidence"][0]["id"]
    status, excerpt = api.handle("GET", f"evidence/{evidence_id}?rkp_hash={h}")
    assert status == 200 and excerpt["status"] == "ok" and excerpt["text"]


def test_a_fresh_clone_is_hydrated_and_reads_evidence_from_the_working_tree(repo):
    h = bundle.read_manifest(repo)["rkp_hash"]
    api = local_api.LocalApi(repo)
    _, body = api.handle("POST", "knowledge/query", {"rkp_hash": h, "seed_selector": {"kind": "Package", "name": "orders-api"}, "max_depth": 1})
    evidence = body["evidence"][0]
    shutil.rmtree(repo / ".rkp" / "cache")

    fresh = local_api.LocalApi(repo)
    status, again = fresh.handle("POST", "knowledge/query", {"rkp_hash": h, "seed_selector": {"kind": "Package", "name": "orders-api"}, "max_depth": 1})
    assert status == 200 and again == body
    status, excerpt = fresh.handle("GET", f"evidence/{evidence['id']}?rkp_hash={h}")
    assert status == 200 and excerpt["status"] == "ok" and excerpt["text"]
    status, exclusions = fresh.handle("GET", f"snapshots/{h}/exclusions")
    assert status == 200 and exclusions["policy_digest"] is None

    # Once the file changes, the analysed content is gone: say so, do not show the new text.
    path = repo / excerpt["paths"][0]
    path.write_text(path.read_text() + "\n# changed\n")
    status, gone = local_api.LocalApi(repo).handle("GET", f"evidence/{evidence['id']}?rkp_hash={h}")
    assert status == 200 and gone["status"] == "source_unavailable"


def test_overrides_round_trip_through_the_repository(repo):
    api = local_api.LocalApi(repo)
    status, current = api.handle("GET", "repositories/local/repo/overrides")
    assert status == 200 and current["version"] == 0 and current["decisions"] == []
    _, detail = api.handle("GET", f"snapshots/{current and bundle.read_manifest(repo)['rkp_hash']}/entities?kind=Component&limit=1")
    component = detail["entities"][0]["id"]
    change = {"decisions": [{"spec": "dm.component-kind", "scope_entity": component, "label": "service", "reason": "owner"}], "preferences": [], "expected_version": 0}
    status, saved = api.handle("PUT", "repositories/local/repo/overrides", change)
    assert status == 200 and saved["version"] > 0
    assert json.loads((repo / ".rkp" / "overrides.json").read_text())["decisions"][0]["label"] == "service"
    assert bundle.bundle_status(repo)["reasons"] == ["owner overrides changed"]
    status, stale = api.handle("PUT", "repositories/local/repo/overrides", change)
    assert status == 409 and stale["code"] == "stale"
    status, invalid = api.handle("PUT", "repositories/local/repo/overrides", {"decisions": [{"spec": "bad"}], "expected_version": saved["version"]})
    assert status == 422


def test_serve_command_reports_a_missing_bundle(tmp_path, capsys, monkeypatch):
    path = tmp_path / "repo"
    materialize("F01-python-api", path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"id": 1, "path": "local"}) + "\n"))
    assert cli.main(["serve", str(path), "--stdio"]) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["status"] == 404 and reply["body"]["code"] == "missing"


@pytest.mark.parametrize("fixture", ["F01-python-api", "F16-polyglot-contracts"])
def test_an_architecture_clone_answers_like_the_full_snapshot_without_detail(tmp_path, fixture):
    path = tmp_path / "repo"
    materialize(fixture, path)
    document = bundle.compile_bundle(path)
    full = local_api.Snapshot(bundle.store_for(path), Manifest.model_validate(document["snapshot"]))
    symbols = {i for i, rows in full.entity_rows.items() if rows[0][1]["kind"] == "Symbol"}
    shutil.rmtree(path / ".rkp" / "cache")

    clone = local_api.LocalApi(path).snapshot()
    assert clone.detail == "architecture" and set(clone.entity_rows) == set(full.entity_rows) - symbols
    counts = full.count_by_kind()
    counts.pop("Symbol", None)
    assert clone.count_by_kind() == counts
    for entity_id in clone.entity_rows:
        want = full.entity_detail(entity_id, limit=100_000)
        got = clone.entity_detail(entity_id, limit=100_000)
        assert got["entity"] == want["entity"], entity_id  # the same row wins when a unit repeats it
        assert got["assertions"] == [a for a in want["assertions"] if not {a["subject"], a["object"]} & symbols], entity_id


def test_rank_decides_which_units_row_of_an_entity_wins(tmp_path):
    """An architecture clone's shard digests differ from the compiled ones; rows must still be
    ordered by the compiled digests, as the projection orders them."""
    path = tmp_path / "repo"
    materialize("F07-event-driven", path)
    document = bundle.compile_bundle(path)
    shutil.rmtree(path / ".rkp" / "cache")
    store, manifest = bundle.hydrate(path)
    units = [ref.unit for ref in manifest.shards]
    forward = local_api.Snapshot(store, manifest, {u: f"{i:04}" for i, u in enumerate(units)})
    backward = local_api.Snapshot(store, manifest, {u: f"{len(units) - i:04}" for i, u in enumerate(units)})
    repeated = [i for i, rows in forward.entity_rows.items() if len(rows) > 1 and rows[0][1] != rows[-1][1]]
    assert repeated
    for entity_id in repeated:
        assert forward.first(entity_id) == backward.entity_rows[entity_id][-1][1] != backward.first(entity_id)


def test_behaviour_answers_with_transform_shapes_and_shapes_alone_are_an_answer(tmp_path):
    path = tmp_path / "repo"
    materialize("F24-transform-shapes", path)
    h = bundle.compile_bundle(path)["rkp_hash"]
    api = local_api.LocalApi(path)
    snap = api.snapshot()

    def find(kind, **key):
        return next(i for i, rows in snap.entity_rows.items() if rows[0][1]["kind"] == kind and rows[0][1]["natural_key"] == key)

    notebook = find("Notebook", path="notebooks/orders_daily.py")
    status, body = api.handle("GET", f"snapshots/{h}/behavior/{notebook}")
    assert status == 200, "a PySpark notebook has shapes and no behaviour units"
    assert body["units"] == [] and body["summary"]["units"] == 0
    assert [r["language"] for r in body["transforms"]] == ["pyspark", "sql"] and {r["owner"] for r in body["transforms"]} == {notebook}

    program = find("Program", platform="sas", name="sas/programs/monthly_orders.sas")
    status, body = api.handle("GET", f"snapshots/{h}/behavior/{program}")
    assert status == 200 and body["units"] and len(body["transforms"]) == 6, "behaviour and shapes side by side"

    table = find("Table", datastore="default", name="stg.orders_latest")
    status, body = api.handle("GET", f"snapshots/{h}/behavior/{table}")
    assert status == 404 and body["code"] == "no_behavior"
