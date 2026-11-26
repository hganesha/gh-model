"""M3 lifecycle: migrations and rebuild (GI-012), triggers and coalescing (GI-018),
retraction, retention and deletion (GI-021, GI-006). Acceptance tests 12 and 14."""

import hashlib
import hmac
import json
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import psycopg
import pytest
from fastapi.testclient import TestClient

from repo_intel.storage import gc, migrations
from repo_intel.knowledge import projection, registry
from repo_intel.server.api import create_app
from repo_intel.compile.fixtures import materialize
from repo_intel.compile.pipeline import load_manifest
from repo_intel.knowledge.rebuild_projection import rebuild
from repo_intel.server.service import TERMINAL, Service
from repo_intel.storage.store import ObjectStore

TOKEN = "test-token-for-local-cluster-only"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
HOOK_KEY = "webhook-secret-for-tests"


@pytest.fixture
def system(tmp_path, pg_conninfo):
    with psycopg.connect(pg_conninfo) as admin:
        projection.ensure_schema(admin)
        projection.truncate_all(admin)
    app = pg_conninfo.replace("user=rkp ", f"user={projection.APP_ROLE} ")
    store = ObjectStore(tmp_path / "objects", "tenant-a")
    service = Service(store, tmp_path / "objects", app, allow_local_paths=True)
    client = TestClient(create_app(app, "tenant-a", TOKEN, store, service))
    return client, service, store


def _settle(service, jobs, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(j["status"] in TERMINAL for j in jobs) and not any(s["active"] or s["pending"] for s in service.ref_slots.values()):
            return jobs
        time.sleep(0.05)
    raise AssertionError([j["status"] for j in jobs])


def _local(service, path, sha):
    return _settle(service, [service.connect(local_path=str(path), ref=sha)])[0]


def _commit(path, n):
    (path / "NOTES.md").write_text(f"note {n}\n")
    subprocess.run(["git", "-C", str(path), "add", "NOTES.md"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", f"note {n}"], check=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


# -- GI-012


def test_migrations_are_numbered_recorded_and_idempotent(pg_conninfo):
    with psycopg.connect(pg_conninfo) as conn:
        projection.ensure_schema(conn)
        assert projection.ensure_schema(conn) == [], "nothing left to apply"
        versions = [v for (v,) in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        assert versions == [v for v, _, _ in migrations.available()] == [1, 2]
        assert conn.execute("SELECT to_regclass('business_lineage')").fetchone()[0] is None
        tables = {t for (t,) in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'schema_migrations'")}
        assert tables == set(migrations.GRANTS), "every table has its grants and row-level security"
        forced = {t for (t,) in conn.execute("SELECT relname FROM pg_class WHERE relforcerowsecurity")}
        assert set(migrations.GRANTS) <= forced


def test_legacy_database_is_adopted_and_business_lineage_migrates(pg_conninfo):
    with psycopg.connect(pg_conninfo, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS legacy")
        admin.execute("CREATE DATABASE legacy")
    legacy = pg_conninfo.replace("dbname=postgres", "dbname=legacy")
    with psycopg.connect(legacy) as conn:
        conn.execute(migrations.available()[0][2])  # the schema as the old ensure_schema left it, without a migrations table
        conn.execute("INSERT INTO snapshots VALUES ('t','h1','github.com/1','c','complete','{}')")
        conn.execute("INSERT INTO business_lineage VALUES ('t','h1','ent_new','ent_old'), ('t','h1','ent_same','ent_same')")
        conn.commit()
        assert projection.ensure_schema(conn) == [1, 2]
        rows = conn.execute("SELECT tenant_id, repository_id, rkp_hash, entity_id, lineage_id, basis FROM entity_lineage").fetchall()
    assert rows == [("t", "github.com/1", "h1", "ent_new", "ent_old", "shared_evidence")]


def test_app_role_deletes_only_through_the_maintenance_functions(system, f01, pg_conninfo):
    _, service, store = system
    repo, shas = f01
    _local(service, repo, shas[0])
    with projection.connect(service.conninfo, "tenant-a") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM snapshots")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE snapshots SET manifest='{}'")
        conn.rollback()
    with psycopg.connect(service.conninfo) as unbound:
        with pytest.raises(psycopg.errors.RaiseException):
            unbound.execute("SELECT rkp_purge_tenant()")
    with projection.connect(service.conninfo, "tenant-b") as other:
        assert projection.purge_tenant(other) == 0, "another tenant's purge cannot reach these rows"
    with psycopg.connect(pg_conninfo) as admin:
        assert admin.execute("SELECT count(*) FROM snapshots WHERE tenant_id='tenant-a'").fetchone()[0] == 1


def test_projection_rebuilds_from_the_store(system, f01, pg_conninfo):
    _, service, store = system
    repo, shas = f01
    jobs = [_local(service, repo, sha) for sha in shas[:3]]
    service.retract(jobs[0]["repository_id"], jobs[0]["rkp_hash"], "compiler_defect", "admin")
    with psycopg.connect(pg_conninfo) as conn:
        before = projection.dump(conn, "tenant-a")
        assert {r[4] for r in before["snapshots"]} == {"complete", "retracted"}
        conn.execute("SELECT set_config('rkp.tenant_id','tenant-a',false)")
        conn.execute("DELETE FROM entities WHERE tenant_id='tenant-a'")
        conn.commit()
        result = rebuild(conn, store)
        assert result["snapshots"] == 2 and result["tombstones"] == 1
        assert projection.dump(conn, "tenant-a") == before
    run = subprocess.run([sys.executable, "-m", "repo_intel.knowledge.rebuild_projection", "--tenant", "tenant-a", "--store-root", str(store.root.parent), "--conninfo", pg_conninfo, "--dry-run"], capture_output=True, text=True)
    assert run.returncode == 0 and "3 snapshot(s)" in run.stdout


# -- GI-018


def _push_tracked(client, service, repo_id="github.com/303", refs=("main",), **tracking):
    document = client.get("/v1/settings", headers=AUTH).json()["settings"]
    document["connections"] = [{
        "id": "app", "name": "App", "mode": "github_app", "host": "github.com", "app_id": "101", "installation_id": "202",
        "credential_ref": "keychain:app-key", "webhook_ref": "keychain:hook-secret",
    }]
    saved = client.post("/v1/settings", headers=AUTH, json=document | {"expected_version": 0}).json()
    service._update(repo_id, full_name="example/repo", source="github_app", connection_id="app", default_branch="main", private=True)
    document["repositories"] = {repo_id: {"refs": list(refs), "trigger": "push", **tracking}}
    client.post("/v1/settings", headers=AUTH, json=document | {"expected_version": saved["version"]})
    return repo_id


def _deliver(client, event, payload, delivery):
    data = json.dumps(payload | {"installation": {"id": 202}, "repository": {"id": 303}}).encode()
    headers = {"X-GitHub-Delivery": delivery, "X-GitHub-Event": event, "X-Hub-Signature-256": "sha256=" + hmac.new(HOOK_KEY.encode(), data, hashlib.sha256).hexdigest()}
    return client.post("/v1/webhooks/app", headers=headers, content=data)


class Fetcher:
    """A connector over a local repository whose first fetch waits until released."""

    def __init__(self, path, head):
        self.path, self.head, self.gate, self.fetched = path, head, threading.Event(), []

    def resolve(self, info, ref):
        return (ref if len(ref) == 40 else self.head), None

    def fetch(self, info, sha, mirrors):
        self.fetched.append(sha)
        assert self.gate.wait(30)
        return self.path


def test_acceptance_12_rapid_pushes_and_redelivery_publish_one_snapshot(system, tmp_path):
    client, service, _ = system
    origin = tmp_path / "origin"
    shas = materialize("F01-python-api", origin)[:1] + [_commit(origin, n) for n in range(9)]
    repo_id = _push_tracked(client, service)
    fetcher = Fetcher(origin, shas[-1])
    with patch.object(service.secrets, "get", return_value=HOOK_KEY), patch.object(service, "_remote", return_value=(fetcher, None)):
        replies = []
        for n, sha in enumerate(shas):
            for attempt in range(2):  # GitHub redelivers with the same delivery ID
                replies.append(_deliver(client, "push", {"ref": "refs/heads/main", "after": sha}, f"delivery-{n}").json())
        again = _deliver(client, "push", {"ref": "refs/heads/main", "after": shas[-1]}, "delivery-late").json()
        fetcher.gate.set()
        jobs = _settle(service, list(service.jobs.values()))
    assert [r["status"] for r in replies].count("duplicate") == len(shas)
    assert again["status"] == "queued" and again["job_id"] == replies[-2]["job_id"], "a head already queued is joined, not compiled twice"
    statuses = sorted(j["status"] for j in jobs)
    assert statuses.count("complete") == 1 and statuses.count("cancelled") == 1 and statuses.count("superseded") == len(shas) - 2
    assert fetcher.fetched == [shas[0], shas[-1]], "superseded heads never start"
    with projection.connect(service.conninfo, "tenant-a") as conn:
        rows = projection.snapshot_rows(conn, "tenant-a")
        ref = registry.tracked_ref(conn, "tenant-a", repo_id, "main")
    assert [r["commit_sha"] for r in rows] == [shas[-1]]
    assert ref["state"] == "current" and ref["head_sha"] == shas[-1] == ref["published_sha"]
    view = client.get(f"/v1/repositories/{repo_id}/rkp", params={"ref": "main"}, headers=AUTH).json()
    assert view["commit_sha"] == shas[-1] and view["ref_view"]["state"] == "current"


def test_a_tagged_commit_is_not_cancelled(system, tmp_path):
    client, service, _ = system
    origin = tmp_path / "origin"
    shas = materialize("F01-python-api", origin)[:2]
    repo_id = _push_tracked(client, service)
    fetcher = Fetcher(origin, shas[-1])
    with patch.object(service.secrets, "get", return_value=HOOK_KEY), patch.object(service, "_remote", return_value=(fetcher, None)):
        with projection.connect(service.conninfo, "tenant-a") as conn:
            registry.track_head(conn, "tenant-a", repo_id, "refs/tags/v1.0", shas[0])
        _deliver(client, "push", {"ref": "refs/heads/main", "after": shas[0]}, "d1")
        _deliver(client, "push", {"ref": "refs/heads/main", "after": shas[1]}, "d2")
        fetcher.gate.set()
        jobs = _settle(service, list(service.jobs.values()))
    assert sorted(j["status"] for j in jobs) == ["complete", "complete"]


def test_tags_compile_eagerly_and_pull_requests_lazily(system, tmp_path):
    client, service, _ = system
    origin = tmp_path / "origin"
    shas = materialize("F01-python-api", origin)
    repo_id = _push_tracked(client, service)
    service.pr_debounce = 0.3
    fetcher = Fetcher(origin, shas[0])
    fetcher.gate.set()
    with patch.object(service.secrets, "get", return_value=HOOK_KEY), patch.object(service, "_remote", return_value=(fetcher, None)):
        tag = _deliver(client, "push", {"ref": "refs/tags/v1.0", "after": shas[0]}, "t1").json()
        assert tag["status"] == "queued"
        _settle(service, [service.jobs[tag["job_id"]]])
        pr = {"action": "synchronize", "pull_request": {"number": 7, "head": {"sha": shas[1]}}}
        assert _deliver(client, "pull_request", pr, "p1").json()["status"] == "recorded"
        assert _deliver(client, "pull_request", pr | {"action": "labeled"}, "p2").json()["status"] == "ignored"
        time.sleep(0.5)
        assert len(service.jobs) == 1, "a lazy pull request does not compile until someone asks"
        ref = "refs/pull/7/head"
        first = client.get(f"/v1/repositories/{repo_id}/rkp", params={"ref": ref}, headers=AUTH)
        assert first.status_code == 202 and first.json()["ref_view"]["state"] == "stale"
        deadline = time.time() + 30
        while time.time() < deadline and client.get(f"/v1/repositories/{repo_id}/rkp", params={"ref": ref}, headers=AUTH).status_code != 200:
            time.sleep(0.1)
        view = client.get(f"/v1/repositories/{repo_id}/rkp", params={"ref": ref}, headers=AUTH).json()
        assert view["commit_sha"] == shas[1] and view["ref_view"] == {"ref": ref, "kind": "pull_request", "state": "current", "head_sha": shas[1], "commit_sha": shas[1], "closed": False}
        moved = _deliver(client, "pull_request", pr | {"pull_request": {"number": 7, "head": {"sha": shas[2]}}}, "p3").json()
        assert moved["status"] == "scheduled", "someone asked for this pull request, so later updates compile after the debounce"
        assert client.get(f"/v1/repositories/{repo_id}/rkp", params={"ref": ref}, headers=AUTH).json()["ref_view"]["state"] == "stale"
        assert _deliver(client, "pull_request", pr | {"action": "closed"}, "p4").json()["status"] == "closed"
        time.sleep(0.6)
        _settle(service, list(service.jobs.values()))
    tagged = client.get(f"/v1/repositories/{repo_id}/rkp", params={"ref": "refs/tags/v1.0"}, headers=AUTH).json()
    assert tagged["commit_sha"] == shas[0] and tagged["ref_view"]["kind"] == "tag"
    with projection.connect(service.conninfo, "tenant-a") as conn:
        closed = registry.tracked_ref(conn, "tenant-a", repo_id, ref)
    assert closed["closed_at"] and closed["published_sha"] == shas[1], "closing cancels the pending compile"


def test_reconciliation_compiles_a_missed_push(system, tmp_path):
    client, service, _ = system
    origin = tmp_path / "origin"
    shas = materialize("F01-python-api", origin)[:2]
    repo_id = _push_tracked(client, service)
    fetcher = Fetcher(origin, shas[0])
    fetcher.gate.set()
    with patch.object(service, "_remote", return_value=(fetcher, None)):
        started = service.reconcile_once()
        assert [j["commit_sha"] for j in started] == [shas[0]]
        _settle(service, started)
        assert service.reconcile_once() == [], "not due again yet"
        service.reconcile_due.clear()
        assert service.reconcile_once() == [], "the head is current"
        fetcher.head = shas[1]  # a push whose webhook never arrived
        service.reconcile_due.clear()
        started = service.reconcile_once()
        assert [j["commit_sha"] for j in started] == [shas[1]]
        _settle(service, started)
    with projection.connect(service.conninfo, "tenant-a") as conn:
        assert registry.tracked_ref(conn, "tenant-a", repo_id, "main")["state"] == "current"


# -- GI-021: acceptance test 14


def _strings_in_store(store_root):
    found = []
    for path in store_root.rglob("*"):
        if path.is_file():
            found.append(path.read_bytes())
    return b"\n".join(found)


def test_acceptance_14_retracted_snapshot_is_unreadable(system, f01):
    client, service, _ = system
    repo, shas = f01
    old, new = (_local(service, repo, sha) for sha in shas[:2])
    h, repo_id = old["rkp_hash"], old["repository_id"]
    summary = client.get(f"/v1/snapshots/{h}/summary", headers=AUTH)
    assert summary.status_code == 200
    entity = client.get(f"/v1/snapshots/{h}/entities", headers=AUTH, params={"kind": "Table"}).json()["entities"][0]
    evidence_id = entity["evidence_ids"][0]
    assert client.post(f"/v1/repositories/{repo_id}/snapshots/{h}/retract", json={"reason": "leaked_secret"}).status_code == 401
    retracted = client.post(f"/v1/repositories/{repo_id}/snapshots/{h}/retract", headers=AUTH, json={"reason": "leaked_secret"})
    assert retracted.status_code == 200 and retracted.json()["replacement"] == new["rkp_hash"]
    reads = [
        client.get(f"/v1/snapshots/{h}/summary", headers=AUTH),
        client.get(f"/v1/snapshots/{h}/entities", headers=AUTH),
        client.get(f"/v1/snapshots/{h}/entities/{entity['id']}", headers=AUTH),
        client.get(f"/v1/snapshots/{h}/decisions", headers=AUTH),
        client.get(f"/v1/snapshots/{h}/dependencies", headers=AUTH),
        client.get(f"/v1/snapshots/{h}/placements", headers=AUTH),
        client.get(f"/v1/evidence/{evidence_id}", params={"rkp_hash": h}, headers=AUTH),
        client.post("/v1/knowledge/query", headers=AUTH, json={"rkp_hash": h, "seed_selector": {"kind": "Table"}}),
        client.get("/v1/knowledge/diff", params={"from": h, "to": new["rkp_hash"]}, headers=AUTH),
        client.get(f"/v1/repositories/{repo_id}/rkp", params={"commit_sha": shas[0]}, headers=AUTH),
    ]
    assert [r.status_code for r in reads] == [410] * len(reads)
    assert all(r.json()["details"] == {"reason": "leaked_secret", "replacement": new["rkp_hash"]} for r in reads)
    with projection.connect(service.conninfo, "tenant-a") as conn:
        assert conn.execute("SELECT count(*) FROM snapshot_shards WHERE rkp_hash=%s", (h,)).fetchone()[0] == 0
    assert client.get(f"/v1/snapshots/{new['rkp_hash']}/summary", headers=AUTH).status_code == 200, "the replacement still reads"
    assert service.store.index_get("rkp", h), "the tombstone keeps the manifest of hashes"


def test_acceptance_14_deleted_repository_leaves_no_content(system, f01, tmp_path, pg_conninfo):
    client, service, store = system
    repo, shas = f01
    other = tmp_path / "other"
    other_sha = materialize("F07-event-driven", other)[0]
    jobs = [_local(service, repo, sha) for sha in shas]
    kept = _local(service, other, other_sha)
    repo_id = jobs[0]["repository_id"]
    mirror = service._mirrors / (hashlib.sha256(repo_id.encode()).hexdigest() + ".git")
    mirror.mkdir(parents=True)
    client.post("/v1/business-maps", headers=AUTH, json={"expected_version": 0, "title": "Map", "stages": [{"id": "s", "name": "S", "capabilities": [{"id": "orders", "name": "Orders"}]}]})
    view = client.get("/v1/business-maps", headers=AUTH).json()
    subject = next(s for s in view["subjects"] if s["repository_id"] == repo_id)
    client.post("/v1/business-maps/placements", headers=AUTH, json={"expected_version": 1, "lineage_id": subject["lineage_id"], "action": "confirm", "capability_id": "orders"})
    while service.placement_worker is not None:
        time.sleep(0.05)
    # Names only F01 contains. Not the bare word "customers": the tenant-wide pattern catalogue uses it.
    markers = [b"/customers", b"create_customer", b"display_name"]
    assert all(m in _strings_in_store(store.root) for m in markers)

    assert client.delete(f"/v1/repositories/{repo_id}").status_code == 401
    deleted = client.delete(f"/v1/repositories/{repo_id}", headers=AUTH)
    assert deleted.status_code == 200 and deleted.json()["snapshots"] == len(shas)
    left = _strings_in_store(store.root)
    assert not any(m in left for m in markers), "no object, index entry or mirror of the repository remains"
    assert repo_id.encode() not in left and not mirror.exists()
    with psycopg.connect(pg_conninfo) as admin:
        for (table,) in admin.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'"):
            text = json.dumps([list(map(str, r)) for r in admin.execute(f"SELECT * FROM {table} WHERE tenant_id='tenant-a'" if table != "schema_migrations" else "SELECT * FROM schema_migrations")])
            assert not any(m.decode() in text for m in markers) and repo_id not in text, table
            assert subject["lineage_id"] not in text or table == "audit_events", table
        audit = admin.execute("SELECT action, subject_hash, details FROM audit_events WHERE tenant_id='tenant-a' AND action='repository.deleted'").fetchone()
    assert audit[1].startswith("sha256:") and repo_id not in json.dumps(audit[2])
    assert client.get(f"/v1/snapshots/{kept['rkp_hash']}/summary", headers=AUTH).status_code == 200, "the other repository is untouched"
    assert service.repository(repo_id) is None and client.delete(f"/v1/repositories/{repo_id}", headers=AUTH).status_code == 404
    rerun = Service(store, store.root.parent, service.conninfo, allow_local_paths=True)
    assert rerun.repository(repo_id) is None, "it does not come back from the local registry file"


def test_deleted_tenant_leaves_only_an_audit_row(system, f01, pg_conninfo):
    client, service, store = system
    repo, shas = f01
    _local(service, repo, shas[0])
    assert client.delete("/v1/workspace", params={"confirm": "wrong"}, headers=AUTH).status_code == 422
    assert client.delete("/v1/workspace", params={"confirm": "tenant-a"}, headers=AUTH).json()["status"] == "deleted"
    assert not store.root.exists()
    with psycopg.connect(pg_conninfo) as admin:
        for table in migrations.GRANTS:
            count = admin.execute(f"SELECT count(*) FROM {table} WHERE tenant_id='tenant-a'").fetchone()[0]
            assert count == (1 if table == "audit_events" else 0), table


def test_late_secret_finding_retracts_the_snapshot(system, f01, monkeypatch):
    client, service, store = system
    repo, shas = f01
    job = _local(service, repo, shas[0])
    assert service.rescan_secrets() == []
    from repo_intel.ingest import secrets
    import re
    # A detector added after publish flags a value that the compiled output contains.
    monkeypatch.setattr(secrets, "_DETECTORS", [*secrets._DETECTORS, ("late_rule", re.compile(rb"create_customer"), 0)])
    assert service.rescan_secrets() == [job["rkp_hash"]]
    assert client.get(f"/v1/snapshots/{job['rkp_hash']}/summary", headers=AUTH).json()["details"]["reason"] == "leaked_secret"


# -- retention and the blob cache (GI-021, GI-006)


def test_retention_collects_old_snapshots_and_keeps_the_head(system, f01):
    client, service, store = system
    repo, shas = f01
    jobs = [_local(service, repo, sha) for sha in shas]
    repo_id = jobs[0]["repository_id"]
    old_mirror = service._mirrors / "stale.git"
    old_mirror.mkdir(parents=True)
    future = time.time() + 40 * gc.DAY
    shard_of_first = {s.digest for s in load_manifest(store, rkp_hash=jobs[0]["rkp_hash"]).shards}
    report = service.collect_garbage(now=future, grace_seconds=0)
    # Each local compile tracks its SHA as the ref, so each is a ref's current head and is kept.
    assert set(report["retained_hashes"]) == {j["rkp_hash"] for j in jobs} and report["dropped_snapshots"] == 0
    assert report["mirrors_deleted"] == 1 and not old_mirror.exists()
    assert report["blobs_evicted"] > 0, "blobs unused for longer than the cache TTL are evicted"
    assert client.get(f"/v1/snapshots/{jobs[-1]['rkp_hash']}/summary", headers=AUTH).status_code == 200
    # Untrack the old refs: only the newest snapshot survives retention.
    with projection.connect(service.conninfo, "tenant-a") as conn:
        conn.execute("UPDATE tracked_refs SET rkp_hash=NULL WHERE ref <> %s", (jobs[-1]["ref"],))
        conn.commit()
    report = service.collect_garbage(now=future, grace_seconds=0)
    assert report["retained_hashes"] == [jobs[-1]["rkp_hash"]] and report["dropped_snapshots"] == len(jobs) - 1
    assert client.get(f"/v1/snapshots/{jobs[0]['rkp_hash']}/summary", headers=AUTH).status_code == 404
    assert [s["rkp_hash"] for s in service.repository(repo_id)["snapshots"]] == [jobs[-1]["rkp_hash"]]
    latest = load_manifest(store, rkp_hash=jobs[-1]["rkp_hash"])
    assert all(store.has(s.digest) for s in latest.shards), "kept shards survive"
    assert not any(store.has(d) for d in shard_of_first - {s.digest for s in latest.shards}), "unshared shards are swept"
    again = _local(service, repo, shas[-1])
    assert again["rkp_hash"] == jobs[-1]["rkp_hash"], "evicted blobs are re-admitted from the repository"


def test_gc_forgets_webhook_deliveries_past_the_redelivery_window(system):
    _, service, _ = system
    rows = [("c1", "old", 30), ("c1", "recent", 1), ("c2", "only", 30)]
    with projection.connect(service.conninfo, "tenant-a") as conn:
        for connection, delivery, days in rows:
            conn.execute("INSERT INTO webhook_deliveries (tenant_id,connection_id,delivery_id,received_at) VALUES ('tenant-a',%s,%s,now() - make_interval(days => %s))", (connection, delivery, days))
        conn.commit()
    report = service.collect_garbage(grace_seconds=0)
    assert report["webhook_deliveries_pruned"] == 1
    with projection.connect(service.conninfo, "tenant-a") as conn:
        left = {r[0] for r in conn.execute("SELECT delivery_id FROM webhook_deliveries WHERE tenant_id='tenant-a'")}
    assert left == {"recent", "only"}, "a connection's latest delivery is kept for the settings view"


def test_store_touch_and_sweep(tmp_path):
    store = ObjectStore(tmp_path, "t")
    a, b = store.put_bytes(b"a"), store.put_bytes(b"b")
    import os
    os.utime(store._object_path(a), (1, 1))
    os.utime(store._object_path(b), (1, 1))
    store.touch(b)
    assert store.sweep(time.time() - 60) == [a] and store.has(b)
    store.index_put("step_cache", "k", b)
    path = store._index_path("step_cache", "k")
    os.utime(path, (1, 1))
    store.index_get("step_cache", "k")
    assert path.stat().st_mtime > 1, "reading a cache entry records its use"
    assert [k for k, _, _ in store.list_index("step_cache")] == ["k"] and store.index_delete("step_cache", "k")


def test_business_map_reads_neither_retracted_nor_quarantined_snapshots():
    from repo_intel.workspace import business_graph
    repo = {"default_branch": "main", "snapshots": [
        {"rkp_hash": "a", "ref": "main", "compiled_at": 1},
        {"rkp_hash": "b", "ref": "main", "compiled_at": 2, "quarantined": True},
        {"rkp_hash": "c", "ref": "main", "compiled_at": 3, "retracted": True},
    ]}
    assert [s["rkp_hash"] for _, s in business_graph.latest([repo])] == ["a"]
    assert business_graph.latest([{**repo, "snapshots": repo["snapshots"][1:]}]) == []
