"""KnowledgeDelta between snapshots, on the fixtures' seeded commits."""

import pytest

from conftest import REPO_ID
from repo_intel.delta import knowledge_delta
from repo_intel.fixtures import materialize
from repo_intel.pipeline import compile_commit
from repo_intel.store import ObjectStore


@pytest.fixture(scope="module")
def snaps(tmp_path_factory):
    cache = {}
    def build(name):
        if name not in cache:
            root = tmp_path_factory.mktemp(name)
            shas = materialize(name, root / "repo")
            store = ObjectStore(root / "store", "t")
            cache[name] = (store, [compile_commit(store, root / "repo", REPO_ID, s).manifest for s in shas])
        return cache[name]
    return build


def _d(snaps, name, i, j):
    store, m = snaps(name)
    return knowledge_delta(store, m[i], m[j])


def _ents(d, change):
    return {(r["kind"], r["name"]) for r in d["entities"] if r["change"] == change}


def _edges(d, change, structural=False):
    return {(r["subject"]["name"], r["predicate"], r["object"]["name"]) for r in d["assertions"] if r["change"] == change and r["structural"] == structural}


def test_identical_snapshots_have_an_empty_delta(snaps):
    d = _d(snaps, "F01-python-api", 0, 0)
    assert d["cause"] == "none" and d["entities"] == [] and d["assertions"] == [] and d["impact"] == []
    assert d["summary"]["shards"]["changed"] == [] and d["files"] == {"available": True, "added": [], "removed": [], "modified": []}


def test_added_column_changes_one_shard_and_reports_impact(snaps):
    d = _d(snaps, "F01-python-api", 0, 1)
    assert d["cause"] == "source_change" and d["same_toolchain"] and d["files"]["modified"] == ["app/models.py"]
    assert _ents(d, "added") == {("Field", "display_name")} and _ents(d, "removed") == set()
    assert ("Table", "customers") in _ents(d, "evidence_changed"), "its file was edited; its own attributes did not change"
    assert _ents(d, "changed") == set(), "a decision reference that changed with its inputs is not reported as a difference"
    assert next(r for r in d["entities"] if r["name"] == "display_name")["label"] == "customers"
    assert d["summary"]["shards"]["changed"] == [".", "@global"] or d["summary"]["shards"]["changed"] == ["."]
    impacted = {(r["entity"]["name"], r["via"], r["because"]["name"]) for r in d["impact"]}
    assert ("create_customer", "WRITES_TO", "customers") in impacted and ("list_customers", "READS_FROM", "customers") in impacted, "code that touches the changed table is flagged"
    assert {r["because_change"] for r in d["impact"]} <= {"added", "changed", "evidence_changed", "moved", "relationship_added"}
    assert not any(r["because"]["kind"] in ("Component", "Package", "Repository") for r in d["impact"])


def test_added_dependency(snaps):
    d = _d(snaps, "F01-python-api", 1, 2)
    assert _ents(d, "added") == {("ExternalDependency", "pydantic-settings")}
    assert ("orders-api", "DEPENDS_ON", "pydantic-settings") in _edges(d, "added")
    assert d["summary"]["by_kind"]["ExternalDependency"]["added"] == 1


def test_moved_and_edited_file_is_removed_plus_added_not_a_guess(snaps):
    d = _d(snaps, "F01-python-api", 2, 3)
    assert ("Artifact", "models.py") in _ents(d, "removed") and ("Artifact", "models.py") in _ents(d, "added")
    assert not any(r["kind"] == "Artifact" and r["name"] == "models.py" for r in d["entities"] if r["change"] == "moved"), "content changed, so it is not claimed to be the same file"
    assert ("Table", "customers") not in _ents(d, "removed"), "the table keeps its identity: its key is its name, not its file"
    assert d["files"]["removed"] == ["app/models.py"] and "app/domain/models.py" in d["files"]["added"]


def test_directory_rename_is_reported_as_moved_with_lineage(snaps):
    d = _d(snaps, "F02-ts-monorepo", 2, 3)
    moved = {(r["kind"], r["name"]): r for r in d["entities"] if r["change"] == "moved"}
    assert {("Package", "@acme/ui"), ("Component", "@acme/ui"), ("Artifact", "Button.tsx"), ("Artifact", "index.ts")} <= set(moved)
    pkg = moved[("Package", "@acme/ui")]
    assert pkg["from_key"]["manifest_dir"] == "packages/ui" and pkg["to_key"]["manifest_dir"] == "packages/design" and pkg["previous_id"] != pkg["id"] and pkg["basis"]
    assert ("Package", "@acme/ui") not in _ents(d, "added") | _ents(d, "removed")
    assert ("@acme/web", "DEPENDS_ON", "@acme/ui") not in _edges(d, "added") | _edges(d, "removed"), "an edge to something that only moved is not new"
    assert d["summary"]["entities"]["moved"] >= 4


def test_new_endpoint_shows_up_with_its_consumers(snaps):
    d = _d(snaps, "F02-ts-monorepo", 0, 1)
    assert _ents(d, "added") == {("Endpoint", "DELETE /orders/:id")}
    assert ("@acme/api", "EXPOSES", "DELETE /orders/:id") in _edges(d, "added")
    assert d["summary"]["shards"]["unchanged"] >= 4, "untouched packages keep their shards"


def test_snapshots_of_different_repositories_are_refused(snaps, tmp_path):
    store, m = snaps("F01-python-api")
    other = m[0].model_copy(update={"repository_id": "github.com/other"})
    with pytest.raises(ValueError):
        knowledge_delta(store, m[0], other)


def test_diff_endpoint(snaps, pg_conninfo, tmp_path):
    import psycopg
    from fastapi.testclient import TestClient
    from repo_intel import projection
    from repo_intel.api import create_app
    from repo_intel.service import Service
    store, m = snaps("F01-python-api")
    with psycopg.connect(pg_conninfo) as admin:
        projection.ensure_schema(admin)
        projection.truncate_all(admin)
        for x in m[:2]:
            projection.project_snapshot(admin, store, x)
    c = TestClient(create_app(pg_conninfo, "t", "test-token-for-local-cluster-only", store, Service(store, tmp_path, pg_conninfo)))
    auth = {"Authorization": "Bearer test-token-for-local-cluster-only"}
    r = c.get("/v1/knowledge/diff", params={"from": m[0].rkp_hash, "to": m[1].rkp_hash}, headers=auth)
    assert r.status_code == 200 and r.json()["summary"]["entities"]["added"] == 1 and r.json()["to"]["commit_sha"] == m[1].commit_sha
    assert c.get("/v1/knowledge/diff", params={"from": m[0].rkp_hash, "to": "sha256:" + "0" * 64}, headers=auth).status_code == 404
    assert c.get("/v1/knowledge/diff", params={"from": m[0].rkp_hash, "to": m[1].rkp_hash}).status_code == 401
