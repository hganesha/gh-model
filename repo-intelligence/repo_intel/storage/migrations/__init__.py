"""Numbered SQL migrations for the PostgreSQL schema (GI-012).

Files named `NNNN_name.sql` in this package are applied in order, each in its
own transaction, and recorded in `schema_migrations`. A transaction-scoped
advisory lock serialises concurrent starters. After the migrations, row-level
security and the application role's grants are (re)applied from `GRANTS`, so
the policy is in one place and every table is covered.

The application role gets SELECT and INSERT on content tables and nothing
broader than its writes need: UPDATE only where rows are edited in place, and
no DELETE on projection content. Retention, retraction and deletion go through
the SECURITY DEFINER functions in `0002_lifecycle.sql`, which act on the bound
tenant only.
"""

from __future__ import annotations

import re
from importlib import resources

import psycopg

APP_ROLE = "rkp_app"
TENANT_SETTING = "rkp.tenant_id"
_LOCK = 7_340_111  # pg_advisory_xact_lock key for schema changes

GRANTS: dict[str, str] = {
    # projection of published shards
    "snapshots": "SELECT, INSERT",
    "snapshot_shards": "SELECT, INSERT",
    "shards": "SELECT, INSERT",
    "entities": "SELECT, INSERT",
    "assertions": "SELECT, INSERT",
    "evidence": "SELECT, INSERT",
    # lifecycle
    "tracked_refs": "SELECT, INSERT, UPDATE",
    "entity_lineage": "SELECT, INSERT, UPDATE",
    "parity_incidents": "SELECT, INSERT",
    "audit_events": "SELECT, INSERT",
    # registry and intake
    "repository_registry": "SELECT, INSERT, UPDATE",
    "connection_checks": "SELECT, INSERT, UPDATE",
    "webhook_deliveries": "SELECT, INSERT, DELETE",
    # workspace settings
    "workspace_settings": "SELECT, INSERT, UPDATE",
    "workspace_settings_revisions": "SELECT, INSERT",
    # business map
    "business_maps": "SELECT, INSERT, UPDATE",
    "business_map_stages": "SELECT, INSERT, DELETE",
    "business_map_capabilities": "SELECT, INSERT, DELETE",
    "business_map_placements": "SELECT, INSERT, DELETE",
    "business_map_revisions": "SELECT, INSERT",
}
# Column-level: the snapshot status changes in place (quarantine and promote).
COLUMN_GRANTS = {"snapshots": "UPDATE (status)"}
FUNCTIONS = [
    "rkp_sweep_shards()", "rkp_drop_snapshots(text[])", "rkp_retract_snapshot(text, jsonb)",
    "rkp_purge_repository(text, text[])", "rkp_purge_tenant()",
]


def available() -> list[tuple[int, str, str]]:
    """(version, name, sql) for every migration file, in order."""
    found = []
    for item in resources.files(__package__).iterdir():
        m = re.fullmatch(r"(\d{4})_([a-z0-9_]+)\.sql", item.name)
        if m:
            found.append((int(m[1]), m[2], item.read_text(encoding="utf-8")))
    found.sort()
    if [v for v, _, _ in found] != list(range(1, len(found) + 1)):
        raise RuntimeError("migration numbers must be contiguous from 0001")
    return found


def applied(conn: psycopg.Connection) -> list[int]:
    if conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0] is None:
        return []
    return [v for (v,) in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def migrate(conn: psycopg.Connection) -> list[int]:
    """Apply pending migrations and the access policy. Run as the schema owner."""
    ran = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version int PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
        done = set(applied(conn))
        for version, name, sql in available():
            if version in done:
                continue
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (version, name))
            ran.append(version)
        _apply_access_policy(conn)
    return ran


def _apply_access_policy(conn: psycopg.Connection) -> None:
    conn.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN CREATE ROLE {APP_ROLE} LOGIN; END IF; END $$")
    for table, grants in GRANTS.items():
        conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        conn.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        conn.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        conn.execute(f"CREATE POLICY tenant_isolation ON {table} USING (tenant_id = (SELECT current_setting('{TENANT_SETTING}', true))) WITH CHECK (tenant_id = (SELECT current_setting('{TENANT_SETTING}', true)))")
        conn.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        conn.execute(f"GRANT {grants} ON {table} TO {APP_ROLE}")
    for table, grants in COLUMN_GRANTS.items():
        conn.execute(f"GRANT {grants} ON {table} TO {APP_ROLE}")
    conn.execute(f"REVOKE ALL ON schema_migrations FROM {APP_ROLE}")
    for fn in FUNCTIONS:
        conn.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        conn.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {APP_ROLE}")
