"""Row-level security: the application role cannot cross tenants even with raw SQL."""

import psycopg
import pytest

from conftest import REPO_ID
from repo_intel import projection
from repo_intel.pipeline import compile_commit
from repo_intel.store import ObjectStore


@pytest.fixture
def two_tenants(tmp_path, f01, pg_conninfo):
    repo, shas = f01
    with psycopg.connect(pg_conninfo) as admin:
        projection.ensure_schema(admin)
        projection.truncate_all(admin)
    app = pg_conninfo.replace("user=rkp ", f"user={projection.APP_ROLE} ")
    manifests = {}
    for tenant in ("tenant-a", "tenant-b"):
        store = ObjectStore(tmp_path / "store", tenant)
        manifests[tenant] = compile_commit(store, repo, REPO_ID, shas[0]).manifest
        with projection.connect(app, tenant) as conn:  # the app role writes its own tenant's rows
            projection.project_snapshot(conn, store, manifests[tenant])
    return app, manifests


def test_app_role_sees_only_its_tenant_even_with_raw_sql(two_tenants, pg_conninfo):
    app, manifests = two_tenants
    with psycopg.connect(pg_conninfo) as admin:
        assert {r[0] for r in admin.execute("SELECT DISTINCT tenant_id FROM entities")} == {"tenant-a", "tenant-b"}
    with projection.connect(app, "tenant-a") as conn:
        for table in projection.TABLES:
            assert {r[0] for r in conn.execute(f"SELECT DISTINCT tenant_id FROM {table}")} == {"tenant-a"}
        # a query that "forgets" its tenant filter still cannot reach the other tenant
        assert conn.execute("SELECT count(*) FROM snapshots WHERE tenant_id = 'tenant-b'").fetchone()[0] == 0
        assert projection.get_snapshot(conn, "tenant-b", rkp_hash=manifests["tenant-b"].rkp_hash) is None


def test_no_tenant_bound_means_no_rows_and_no_writes(two_tenants):
    app, _ = two_tenants
    with psycopg.connect(app) as conn:
        assert conn.execute("SELECT count(*) FROM entities").fetchone()[0] == 0
    with projection.connect(app, "tenant-a") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO shards VALUES ('tenant-b', 'sha256:forged')")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM entities")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("ALTER TABLE entities DISABLE ROW LEVEL SECURITY")
