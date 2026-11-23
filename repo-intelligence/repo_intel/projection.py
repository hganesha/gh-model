"""PostgreSQL projection of published shards, and graph queries over it (spec §12).

The shard store is the source of truth; everything here can be rebuilt from it.
Rows are keyed by shard digest, so snapshots that share a shard share its rows.

Every query filters by tenant, and row-level security enforces the same rule
in the database as a backstop. Path-scope visibility (spec §11) is GI-022.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .contracts import Manifest
from .store import ObjectStore

DDL = """
CREATE TABLE IF NOT EXISTS snapshots (
  tenant_id text NOT NULL, rkp_hash text NOT NULL, repository_id text NOT NULL, commit_sha text NOT NULL,
  status text NOT NULL, manifest jsonb NOT NULL, PRIMARY KEY (tenant_id, rkp_hash));
CREATE INDEX IF NOT EXISTS snapshots_commit ON snapshots (tenant_id, repository_id, commit_sha);
CREATE TABLE IF NOT EXISTS snapshot_shards (
  tenant_id text NOT NULL, rkp_hash text NOT NULL, unit text NOT NULL, shard_digest text NOT NULL,
  PRIMARY KEY (tenant_id, rkp_hash, unit));
CREATE TABLE IF NOT EXISTS shards (tenant_id text NOT NULL, shard_digest text NOT NULL, PRIMARY KEY (tenant_id, shard_digest));
CREATE TABLE IF NOT EXISTS entities (
  tenant_id text NOT NULL, shard_digest text NOT NULL, entity_id text NOT NULL, kind text NOT NULL, name text NOT NULL,
  row jsonb NOT NULL, PRIMARY KEY (tenant_id, shard_digest, entity_id));
CREATE TABLE IF NOT EXISTS assertions (
  tenant_id text NOT NULL, shard_digest text NOT NULL, assertion_id text NOT NULL, subject_id text NOT NULL,
  predicate text NOT NULL, object_id text NOT NULL, row jsonb NOT NULL, PRIMARY KEY (tenant_id, shard_digest, assertion_id));
CREATE INDEX IF NOT EXISTS assertions_subject ON assertions (tenant_id, subject_id, predicate);
CREATE INDEX IF NOT EXISTS assertions_object ON assertions (tenant_id, object_id, predicate);
CREATE TABLE IF NOT EXISTS evidence (
  tenant_id text NOT NULL, shard_digest text NOT NULL, evidence_id text NOT NULL, blob_sha256 text NOT NULL,
  row jsonb NOT NULL, PRIMARY KEY (tenant_id, shard_digest, evidence_id));
"""

TABLES = ["snapshots", "snapshot_shards", "shards", "entities", "assertions", "evidence"]


APP_ROLE = "rkp_app"
_TENANT_SETTING = "rkp.tenant_id"


def ensure_schema(conn: psycopg.Connection) -> None:
    """Create tables, row-level security and the application role. Run as the owner.

    Every table carries tenant_id and a policy that compares it with the
    session setting `rkp.tenant_id`. The application role is not a superuser
    and does not own the tables, so it cannot bypass the policy; with the
    setting absent it sees nothing. This is the backstop behind the tenant
    filters in the queries below (spec §11).
    """
    conn.execute(DDL)
    conn.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN CREATE ROLE {APP_ROLE} LOGIN; END IF; END $$")
    for t in TABLES:
        conn.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        conn.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        conn.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        conn.execute(f"CREATE POLICY tenant_isolation ON {t} USING (tenant_id = current_setting('{_TENANT_SETTING}', true)) WITH CHECK (tenant_id = current_setting('{_TENANT_SETTING}', true))")
        conn.execute(f"GRANT SELECT, INSERT ON {t} TO {APP_ROLE}")
    conn.commit()


def connect(conninfo: str, tenant_id: str) -> psycopg.Connection:
    """Open a connection bound to one tenant for row-level security."""
    conn = psycopg.connect(conninfo)
    conn.execute("SELECT set_config(%s, %s, false)", (_TENANT_SETTING, tenant_id))
    conn.commit()
    return conn


def project_snapshot(conn: psycopg.Connection, store: ObjectStore, manifest: Manifest) -> None:
    """Load a published snapshot into the projection. Idempotent."""
    t = manifest.tenant_id
    with conn.transaction():
        conn.execute("INSERT INTO snapshots VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (t, manifest.rkp_hash, manifest.repository_id, manifest.commit_sha, manifest.status, Jsonb(manifest.row())))
        for ref in manifest.shards:
            conn.execute("INSERT INTO snapshot_shards VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (t, manifest.rkp_hash, ref.unit, ref.digest))
            fresh = conn.execute("INSERT INTO shards VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING 1", (t, ref.digest)).fetchone()
            if not fresh:
                continue  # rows for this shard are already present
            shard = store.get_json(ref.digest)
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO entities VALUES (%s,%s,%s,%s,%s,%s)", [(t, ref.digest, e["id"], e["kind"], e["name"], Jsonb(e)) for e in shard["entities"]])
                cur.executemany("INSERT INTO assertions VALUES (%s,%s,%s,%s,%s,%s,%s)", [(t, ref.digest, a["id"], a["subject"], a["predicate"], a["object"], Jsonb(a)) for a in shard["assertions"]])
                cur.executemany("INSERT INTO evidence VALUES (%s,%s,%s,%s,%s)", [(t, ref.digest, v["id"], v["blob_sha256"], Jsonb(v)) for v in shard["evidence"]])


def dump(conn: psycopg.Connection) -> dict[str, list[tuple]]:
    """Whole projection in a stable order; used to check rebuild equivalence."""
    return {t: sorted(conn.execute(f"SELECT * FROM {t}").fetchall(), key=repr) for t in TABLES}


def truncate_all(conn: psycopg.Connection) -> None:
    conn.execute("TRUNCATE " + ", ".join(TABLES))
    conn.commit()


def get_snapshot(conn: psycopg.Connection, tenant_id: str, *, rkp_hash: str | None = None, repository_id: str | None = None, commit_sha: str | None = None) -> dict[str, Any] | None:
    if rkp_hash:
        row = conn.execute("SELECT manifest FROM snapshots WHERE tenant_id=%s AND rkp_hash=%s", (tenant_id, rkp_hash)).fetchone()
    else:
        row = conn.execute("SELECT manifest FROM snapshots WHERE tenant_id=%s AND repository_id=%s AND commit_sha=%s", (tenant_id, repository_id, commit_sha)).fetchone()
    return row[0] if row else None


_IN_SNAPSHOT = "shard_digest IN (SELECT shard_digest FROM snapshot_shards WHERE tenant_id=%(t)s AND rkp_hash=%(h)s)"


def find_entities(conn: psycopg.Connection, tenant_id: str, rkp_hash: str, kind: str | None, name: str | None) -> list[str]:
    q = f"SELECT DISTINCT entity_id FROM entities WHERE tenant_id=%(t)s AND {_IN_SNAPSHOT}"
    p: dict[str, Any] = {"t": tenant_id, "h": rkp_hash}
    if kind:
        q += " AND kind=%(k)s"; p["k"] = kind
    if name:
        q += " AND name=%(n)s"; p["n"] = name
    return [r[0] for r in conn.execute(q + " ORDER BY 1", p)]


def query_graph(conn: psycopg.Connection, tenant_id: str, rkp_hash: str, seeds: list[str], allowed_edge_types: list[str] | None, max_depth: int, max_nodes: int) -> dict[str, Any]:
    """Breadth-first traversal in both directions. Truncation is deterministic:
    nearer nodes first, then by ID."""
    p = {"t": tenant_id, "h": rkp_hash}
    known = {r[0] for r in conn.execute(f"SELECT DISTINCT entity_id FROM entities WHERE tenant_id=%(t)s AND {_IN_SNAPSHOT} AND entity_id = ANY(%(s)s)", {**p, "s": seeds})}
    visited: list[str] = sorted(known)[:max_nodes]
    truncated = len(known) > max_nodes
    seen = set(visited)
    frontier = list(visited)
    edges: dict[str, dict[str, Any]] = {}
    for _ in range(max_depth):
        if not frontier:
            break
        q = f"SELECT row FROM assertions WHERE tenant_id=%(t)s AND {_IN_SNAPSHOT} AND (subject_id = ANY(%(f)s) OR object_id = ANY(%(f)s))"
        params: dict[str, Any] = {**p, "f": frontier}
        if allowed_edge_types is not None:
            q += " AND predicate = ANY(%(e)s)"; params["e"] = allowed_edge_types
        found = [r[0] for r in conn.execute(q, params)]
        nxt = sorted({a[end] for a in found for end in ("subject", "object")} - seen)
        room = max_nodes - len(visited)
        if len(nxt) > room:
            truncated = True
            nxt = nxt[:room]
        seen.update(nxt)
        visited += nxt
        for a in found:
            if a["subject"] in seen and a["object"] in seen:
                edges[a["id"]] = a
        frontier = nxt
    ents = {r[0]["id"]: r[0] for r in conn.execute(f"SELECT row FROM entities WHERE tenant_id=%(t)s AND {_IN_SNAPSHOT} AND entity_id = ANY(%(v)s) ORDER BY shard_digest DESC", {**p, "v": visited})}
    ev_ids = sorted({i for a in edges.values() for i in a["evidence_ids"]} | {i for e in ents.values() for i in e["evidence_ids"]})
    evs = {r[0]["id"]: r[0] for r in conn.execute(f"SELECT row FROM evidence WHERE tenant_id=%(t)s AND {_IN_SNAPSHOT} AND evidence_id = ANY(%(v)s)", {**p, "v": ev_ids})}
    return {"entities": [ents[i] for i in sorted(ents)], "assertions": [edges[i] for i in sorted(edges)], "evidence": [evs[i] for i in sorted(evs)], "truncated": truncated}


def get_evidence(conn: psycopg.Connection, tenant_id: str, rkp_hash: str, evidence_id: str) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT row FROM evidence WHERE tenant_id=%(t)s AND {_IN_SNAPSHOT} AND evidence_id=%(e)s LIMIT 1", {"t": tenant_id, "h": rkp_hash, "e": evidence_id}).fetchone()
    return row[0] if row else None


def count_by_kind(conn: psycopg.Connection, tenant_id: str, rkp_hash: str) -> dict[str, int]:
    q = f"SELECT kind, count(DISTINCT entity_id) FROM entities WHERE tenant_id=%(t)s AND {_IN_SNAPSHOT} GROUP BY kind"
    return {k: n for k, n in conn.execute(q, {"t": tenant_id, "h": rkp_hash})}


def list_entities(conn: psycopg.Connection, tenant_id: str, rkp_hash: str, kinds: list[str] | None, text: str | None, limit: int) -> list[dict[str, Any]]:
    """Entities of a snapshot, optionally filtered by kind and by a name/path substring."""
    q = f"SELECT DISTINCT ON (kind, name, entity_id) row FROM entities WHERE tenant_id=%(t)s AND {_IN_SNAPSHOT}"
    p: dict[str, Any] = {"t": tenant_id, "h": rkp_hash, "limit": limit}
    if kinds:
        q += " AND kind = ANY(%(k)s)"; p["k"] = kinds
    if text:
        q += " AND (name ILIKE %(q)s OR row->'natural_key'->>'path' ILIKE %(q)s)"
        p["q"] = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return [r[0] for r in conn.execute(q + " ORDER BY kind, name, entity_id, shard_digest DESC LIMIT %(limit)s", p)]


def entity_detail(conn: psycopg.Connection, tenant_id: str, rkp_hash: str, entity_id: str, limit: int = 400) -> dict[str, Any] | None:
    p = {"t": tenant_id, "h": rkp_hash, "e": entity_id, "limit": limit}
    rows = [r[0] for r in conn.execute(f"SELECT row FROM entities WHERE tenant_id=%(t)s AND {_IN_SNAPSHOT} AND entity_id=%(e)s ORDER BY shard_digest DESC", p)]
    if not rows:
        return None
    entity = rows[0]
    for other in rows[1:]:  # the same entity declared in several shards: pool what they say
        entity = {**entity, "attributes": {**other["attributes"], **entity["attributes"]}, "evidence_ids": sorted(set(entity["evidence_ids"]) | set(other["evidence_ids"]))}
    found = [r[0] for r in conn.execute(f"SELECT DISTINCT ON (assertion_id) row FROM assertions WHERE tenant_id=%(t)s AND {_IN_SNAPSHOT} AND (subject_id=%(e)s OR object_id=%(e)s) ORDER BY assertion_id LIMIT %(limit)s", p)]
    ids = sorted({a["subject"] for a in found} | {a["object"] for a in found})
    names = {r[0]: {"id": r[0], "kind": r[1], "name": r[2]} for r in conn.execute(f"SELECT DISTINCT entity_id, kind, name FROM entities WHERE tenant_id=%(t)s AND {_IN_SNAPSHOT} AND entity_id = ANY(%(i)s)", {**p, "i": ids})}
    return {"entity": entity, "assertions": found, "neighbours": names, "truncated": len(found) >= limit}
