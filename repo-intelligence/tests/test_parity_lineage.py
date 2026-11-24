"""Parity, quarantine and promote (GI-019); lineage, causes and impact (GI-020).
Acceptance tests 5, 11 and 13, and incremental/cold parity on every seeded fixture commit pair."""

import json
import os
import time
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import REPO_ID
from repo_intel.compile import buildgraph, pipeline
from repo_intel import extractors
from repo_intel.storage import gc
from repo_intel.knowledge import lineage, parity, projection, registry
from repo_intel.server.api import create_app
from repo_intel.compile.buildgraph import Step
from repo_intel.knowledge.delta import knowledge_delta
from repo_intel.compile.fixtures import list_fixtures, materialize
from repo_intel.compile.pipeline import compile_commit, load_manifest
from repo_intel.server.service import TERMINAL, Service
from repo_intel.storage.store import ObjectStore

TOKEN = "test-token-for-local-cluster-only"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def system(tmp_path, pg_conninfo):
    with psycopg.connect(pg_conninfo) as admin:
        projection.ensure_schema(admin)
        projection.truncate_all(admin)
    app = pg_conninfo.replace("user=rkp ", f"user={projection.APP_ROLE} ")
    store = ObjectStore(tmp_path / "objects", "tenant-a")
    service = Service(store, tmp_path / "objects", app, allow_local_paths=True)
    return TestClient(create_app(app, "tenant-a", TOKEN, store, service)), service, store


def _local(service, path, sha, ref=None):
    job = service._start_local(Path(path), ref or sha, sha=sha)  # a named ref, pinned to the commit we mean
    deadline = time.time() + 60
    while job["status"] not in TERMINAL or job.get("shadow") == "running":
        assert time.time() < deadline
        time.sleep(0.05)
    assert job["status"] == "complete", job
    return job


# -- GI-019: parity


@pytest.mark.parametrize("fixture", list_fixtures())
def test_incremental_equals_cold_on_every_seeded_commit_pair(fixture, tmp_path):
    reports = parity.check_fixture(fixture, tmp_path)
    assert len(reports) >= 2 and all(r.match for r in reports), [r.row() for r in reports if not r.match]


def _impure_unit_compile(monkeypatch):
    """A unit.compile that also reads an undeclared input (an environment variable)."""
    original = pipeline.UNIT_COMPILE

    def fn(inputs, config):
        out = original.fn(inputs, config)
        for e in out["entities"]:
            if e["kind"] == "Package":
                e["attributes"]["build_flavour"] = os.environ.get("RKP_IMPURE", "")
        return out

    monkeypatch.setattr(pipeline, "UNIT_COMPILE", Step(original.name, original.version, fn))
    monkeypatch.setenv(buildgraph.EXECUTOR_ENV, "inprocess")  # the impure step exists only in this process


def test_parity_harness_catches_an_injected_impure_step(tmp_path, monkeypatch):
    _impure_unit_compile(monkeypatch)
    root = tmp_path / "f02"
    shas = materialize("F02-ts-monorepo", root / "repo")
    warm, cold = ObjectStore(root / "warm", "t"), ObjectStore(root / "cold", "t")
    monkeypatch.setenv("RKP_IMPURE", "monday")
    compile_commit(warm, root / "repo", REPO_ID, shas[0])
    monkeypatch.setenv("RKP_IMPURE", "tuesday")
    inc = compile_commit(warm, root / "repo", REPO_ID, shas[1]).manifest
    full = compile_commit(cold, root / "repo", REPO_ID, shas[1]).manifest
    report = parity.compare(inc, full, warm, cold)
    assert not report.match
    units = {s["unit"] for s in report.differing_shards}
    assert units and "apps/api" not in units, "the unit the commit changed was recompiled, so it agrees; cached ones do not"
    assert all(s["rows"]["entities"]["changed"] for s in report.differing_shards if "rows" in s)


def test_shadow_compile_quarantines_and_promotes_then_admin_promotes_back(system, tmp_path, monkeypatch):
    client, service, store = system
    _impure_unit_compile(monkeypatch)
    repo = tmp_path / "f02"
    shas = materialize("F02-ts-monorepo", repo)
    monkeypatch.setenv("RKP_IMPURE", "monday")
    _local(service, repo, shas[0], ref="main")
    monkeypatch.setenv("RKP_IMPURE", "tuesday")
    job = _local(service, repo, shas[1], ref="main")
    inc = load_manifest(store, rkp_hash=job["rkp_hash"])
    report = service.shadow_compile(inc, repo)
    assert not report.match and report.differing_shards
    with projection.connect(service.conninfo, "tenant-a") as conn:
        status = {r["rkp_hash"]: r["status"] for r in projection.snapshot_rows(conn, "tenant-a")}
        incident = conn.execute("SELECT incremental_rkp_hash, cold_rkp_hash, differing FROM parity_incidents").fetchone()
        ref = registry.tracked_ref(conn, "tenant-a", job["repository_id"], "main")
    assert status[inc.rkp_hash] == "quarantined" and status[report.cold] == "complete"
    assert incident[0] == inc.rkp_hash and incident[1] == report.cold and incident[2]["differing_shards"]
    assert ref["rkp_hash"] == report.cold, "the cold snapshot is promoted to the ref"
    query = {"rkp_hash": inc.rkp_hash, "seed_selector": {"kind": "Package"}}
    assert client.post("/v1/knowledge/query", headers=AUTH, json=query).status_code == 404, "a quarantined snapshot is not served as complete"
    assert client.get(f"/v1/repositories/{job['repository_id']}/rkp", params={"ref": "main"}, headers=AUTH).json()["rkp_hash"] == report.cold
    promoted = client.post(f"/v1/repositories/{job['repository_id']}/snapshots/{inc.rkp_hash}/promote", headers=AUTH)
    assert promoted.status_code == 200 and promoted.json()["status"] == "complete"
    assert client.post("/v1/knowledge/query", headers=AUTH, json=query).status_code == 200


def test_sampled_shadow_compile_runs_after_publish(system, f01):
    client, service, _ = system
    document = client.get("/v1/settings", headers=AUTH).json()["settings"]
    document["parity"] = {"shadow_rate": 1.0, "nightly_head": False}
    client.post("/v1/settings", headers=AUTH, json=document | {"expected_version": 0})
    repo, shas = f01
    job = _local(service, repo, shas[0])
    assert job["shadow"]["match"] is True and job["shadow"]["incremental"] == job["rkp_hash"] == job["shadow"]["cold"]
    assert parity.sampled(job["rkp_hash"], 0) is False and parity.sampled(job["rkp_hash"], 1) is True


# -- GI-020: acceptance test 5


def _reachable(store, manifest):
    roots, _ = gc._manifest_roots(store, manifest.rkp_hash)
    return {d: store.get_bytes(d) for d in gc._closure(store, roots, set()) if store.has(d)}


def test_acceptance_5_schema_change(system, f01, pg_conninfo):
    client, service, store = system
    repo, shas = f01
    first = _local(service, repo, shas[0], ref="main")
    before = load_manifest(store, rkp_hash=first["rkp_hash"])
    objects = _reachable(store, before)
    with psycopg.connect(pg_conninfo) as conn:
        rows = {t: [r for r in table if before.rkp_hash in map(str, r) or any(s.digest in map(str, r) for s in before.shards)] for t, table in projection.dump(conn, "tenant-a").items()}
    second = _local(service, repo, shas[1], ref="main")
    after = load_manifest(store, rkp_hash=second["rkp_hash"])
    assert _reachable(store, before) == objects, "every object of the previous snapshot is byte-identical"
    assert load_manifest(store, rkp_hash=before.rkp_hash) == before
    with psycopg.connect(pg_conninfo) as conn:
        now = projection.dump(conn, "tenant-a")
    assert all(row in now[t] for t, r in rows.items() for row in r) and rows["entities"], "and so are its projection rows"
    d = client.get("/v1/knowledge/diff", params={"from": before.rkp_hash, "to": after.rkp_hash}, headers=AUTH).json()
    assert d["cause"] == "source_change" and d["toolchain_changes"] == {}
    added = [r for r in d["entities"] if r["change"] == "added"]
    assert [(r["kind"], r["name"], r["cause"]) for r in added] == [("Field", "display_name", "source_change")]
    assert d["summary"]["shards"]["changed"] and set(d["summary"]["shards"]["changed"]) <= {".", "@global"}
    assert d["summary"]["causes"] == {"source_change": len(d["entities"]) + len(d["assertions"])}


def test_impact_reaches_tests_over_several_hops(tmp_path):
    store = ObjectStore(tmp_path, "t")
    shas = materialize("F01-python-api", tmp_path / "repo")
    a, b = (compile_commit(store, tmp_path / "repo", REPO_ID, s).manifest for s in shas[:2])
    d = knowledge_delta(store, a, b)
    hops = {(r["entity"]["name"], r["via"], r["hops"]) for r in d["impact"]}
    assert ("create_customer", "WRITES_TO", 1) in hops
    further = [r for r in d["impact"] if r["hops"] > 1]
    assert further and all(r["through"] and r["because"]["name"] in ("customers", "display_name") for r in further)
    assert d["tests"] and any(r["entity"]["kind"] == "Test" and r["via"] == "TESTED_BY" and r["hops"] >= 2 for r in d["impact"]), "the tests of affected code are reached through TESTED_BY"


# -- GI-020: acceptance test 11 (rename), with lineage and explicit parity


def test_acceptance_11_component_rename(system, tmp_path):
    client, service, store = system
    repo = tmp_path / "f02"
    shas = materialize("F02-ts-monorepo", repo)
    _local(service, repo, shas[2], ref="main")
    job = _local(service, repo, shas[3], ref="main")
    before, after = load_manifest(store, rkp_hash=service.repository(job["repository_id"])["snapshots"][0]["rkp_hash"]), load_manifest(store, rkp_hash=job["rkp_hash"])
    d = client.get("/v1/knowledge/diff", params={"from": before.rkp_hash, "to": after.rkp_hash}, headers=AUTH).json()
    moved = {(r["kind"], r["name"]): r for r in d["entities"] if r["change"] == "moved"}
    pkg = moved[("Package", "@acme/ui")]
    assert pkg["previous_id"] != pkg["id"] and pkg["to_key"]["manifest_dir"] == "packages/design"
    with projection.connect(service.conninfo, "tenant-a") as conn:
        links = dict(conn.execute("SELECT entity_id, lineage_id FROM entity_lineage WHERE rkp_hash=%s", (after.rkp_hash,)).fetchall())
    assert links[pkg["id"]] == pkg["previous_id"] and links[moved[("Component", "@acme/ui")]["id"]] == moved[("Component", "@acme/ui")]["previous_id"]
    history = client.get(f"/v1/entities/{pkg['id']}/lineage", headers=AUTH).json()
    assert history["lineage_id"] == pkg["previous_id"] and [r["entity_id"] for r in history["revisions"]] == [pkg["previous_id"], pkg["id"]]
    assert history["revisions"][1]["moved_from"] == pkg["previous_id"] and history["revisions"][1]["basis"]
    assert client.get(f"/v1/entities/{pkg['previous_id']}/lineage", headers=AUTH).json()["lineage_id"] == pkg["previous_id"]
    assert client.get("/v1/entities/ent_unknown/lineage", headers=AUTH).status_code == 404
    cold = compile_commit(ObjectStore(tmp_path / "cold", "tenant-a"), repo, job["repository_id"], shas[3]).manifest
    assert parity.compare(after, cold).match and after.content_hash == cold.content_hash, "incremental and cold agree across the rename"


def test_git_rename_detection_links_a_moved_and_edited_file(system, f01):
    client, service, store = system
    repo, shas = f01
    _local(service, repo, shas[2], ref="main")
    job = _local(service, repo, shas[3], ref="main")
    renames = lineage.git_renames(repo, shas[2], shas[3])
    assert renames["app/models.py"][0] == "app/domain/models.py" and renames["app/models.py"][1] < 100
    before = service.repository(job["repository_id"])["snapshots"][0]["rkp_hash"]
    d = client.get("/v1/knowledge/diff", params={"from": before, "to": job["rkp_hash"]}, headers=AUTH).json()
    moved = next(r for r in d["entities"] if r["change"] == "moved" and r["kind"] == "Artifact" and r["name"] == "models.py")
    assert moved["basis"].startswith("git rename detection") and moved["to_key"] == {"path": "app/domain/models.py"}
    assert lineage.directory_moves({"packages/ui/src/a.ts": ("packages/design/src/a.ts", 100)}) == {"packages/ui": "packages/design"}


# -- GI-020: acceptance test 13 (toolchain upgrade)


def test_acceptance_13_extractor_upgrade_is_attributed_to_extractor_change(tmp_path, monkeypatch):
    from repo_intel.extractors.base import Extraction

    monkeypatch.setenv(buildgraph.EXECUTOR_ENV, "inprocess")  # the upgrade below exists only in this process
    store = ObjectStore(tmp_path / "s", "t")
    shas = materialize("F01-python-api", tmp_path / "repo")
    first = compile_commit(store, tmp_path / "repo", REPO_ID, shas[0])
    ext = next(e for e in extractors.EXTRACTORS if e.id == "manifest.pyproject")
    original = ext.extract

    def upgraded(path, data, blob):  # the upgraded version stops reporting httpx
        r = original(path, data, blob)
        return Extraction([o for o in r.observations if "httpx" not in json.dumps(o.row())], r.diagnostics, r.parsed)

    monkeypatch.setattr(ext, "version", ext.version + ".1")  # changes this extractor's version, and so only its files' keys
    monkeypatch.setattr(ext, "extract", upgraded)
    second = compile_commit(store, tmp_path / "repo", REPO_ID, shas[0])
    d = knowledge_delta(store, first.manifest, second.manifest)
    assert d["same_source"] and d["cause"] == "extractor_change" and list(d["toolchain_changes"]) == ["extractor_change"]
    assert ("ExternalDependency", "httpx") in {(r["kind"], r["name"]) for r in d["entities"] if r["change"] == "removed"}
    assert d["entities"] and {r["cause"] for r in [*d["entities"], *d["assertions"]]} == {"extractor_change"}
    entries = store.get_json(store.index_get("path_manifests", second.manifest.source_tree_hash))["entries"]
    matched = sum(ext.matches(e["path"]) for e in entries)
    assert 0 < matched < len(entries)
    assert second.stats.executed["file.extract"] == matched, "only the upgraded extractor's files re-extract"
