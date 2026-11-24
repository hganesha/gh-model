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

from . import migrations
from .contracts import Manifest
from .store import ObjectStore

TABLES = ["snapshots", "snapshot_shards", "shards", "entities", "assertions", "evidence"]
APP_ROLE = migrations.APP_ROLE
_TENANT_SETTING = migrations.TENANT_SETTING


def ensure_schema(conn: psycopg.Connection) -> list[int]:
    """Apply pending migrations, row-level security and grants. Run as the owner.

    Every table carries tenant_id and a policy that compares it with the
    session setting `rkp.tenant_id`. The application role is not a superuser
    and does not own the tables, so it cannot bypass the policy; with the
    setting absent it sees nothing. This is the backstop behind the tenant
    filters in the queries below (spec §11).
    """
    ran = migrations.migrate(conn)
    conn.commit()
    return ran


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


def dump(conn: psycopg.Connection, tenant_id: str | None = None) -> dict[str, list[tuple]]:
    """Whole projection in a stable order; used to check rebuild equivalence.
    `published_at` is bookkeeping (when the row was written) and is left out."""
    where = " WHERE tenant_id = %s" if tenant_id else ""
    cols = {"snapshots": "tenant_id, rkp_hash, repository_id, commit_sha, status, manifest, retraction"}
    return {t: sorted(conn.execute(f"SELECT {cols.get(t, '*')} FROM {t}{where}", (tenant_id,) if tenant_id else ()).fetchall(), key=repr) for t in TABLES}


def truncate_all(conn: psycopg.Connection) -> None:
    """Empty every tenant table (tests and local resets). Run as the owner."""
    conn.execute("TRUNCATE " + ", ".join(migrations.GRANTS))
    conn.commit()


def get_snapshot(conn: psycopg.Connection, tenant_id: str, *, rkp_hash: str | None = None, repository_id: str | None = None, commit_sha: str | None = None, toolchain_hash: str | None = None) -> dict[str, Any] | None:
    """The manifest of one snapshot, with `status` as it is now (a published
    manifest says `complete`; quarantine and retraction change the row)."""
    if rkp_hash:
        row = conn.execute("SELECT manifest || jsonb_build_object('status', status) FROM snapshots WHERE tenant_id=%s AND rkp_hash=%s", (tenant_id, rkp_hash)).fetchone()
    else:
        row = conn.execute(
            """SELECT s.manifest || jsonb_build_object('status', s.status) FROM snapshots s
               LEFT JOIN repository_registry r USING (tenant_id, repository_id)
               LEFT JOIN LATERAL (
                 SELECT max((snap->>'compiled_at')::double precision) AS published
                 FROM jsonb_array_elements(r.document->'snapshots') snap
                 WHERE snap->>'rkp_hash' = s.rkp_hash
               ) last_compile ON true
               WHERE s.tenant_id=%s AND s.repository_id=%s AND s.commit_sha=%s
                 AND (%s::text IS NULL OR s.manifest->>'toolchain_hash' = %s)
               ORDER BY last_compile.published DESC NULLS LAST, s.rkp_hash LIMIT 1""",
            (tenant_id, repository_id, commit_sha, toolchain_hash, toolchain_hash),
        ).fetchone()
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


# -- lifecycle: status, retraction, retention and deletion (GI-019, GI-021)


def retraction(conn: psycopg.Connection, tenant_id: str, rkp_hash: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT retraction FROM snapshots WHERE tenant_id=%s AND rkp_hash=%s AND status='retracted'", (tenant_id, rkp_hash)).fetchone()
    return (row[0] or {}) if row else None


def set_status(conn: psycopg.Connection, tenant_id: str, rkp_hash: str, status: str) -> bool:
    """Quarantine or restore a published snapshot. Retraction is final and goes through `retract`."""
    if status not in ("complete", "quarantined"):
        raise ValueError(status)
    with conn.transaction():
        return conn.execute("UPDATE snapshots SET status=%s WHERE tenant_id=%s AND rkp_hash=%s AND status IN ('complete','quarantined') RETURNING 1", (status, tenant_id, rkp_hash)).fetchone() is not None


def retract(conn: psycopg.Connection, tenant_id: str, rkp_hash: str, record: dict[str, Any]) -> bool:
    with conn.transaction():
        return bool(conn.execute("SELECT rkp_retract_snapshot(%s, %s)", (rkp_hash, Jsonb(record))).fetchone()[0])


def drop_snapshots(conn: psycopg.Connection, rkp_hashes: list[str]) -> int:
    """Remove snapshots from the projection, with any shard rows no other snapshot uses."""
    if not rkp_hashes:
        return 0
    with conn.transaction():
        return conn.execute("SELECT rkp_drop_snapshots(%s)", (sorted(rkp_hashes),)).fetchone()[0]


def purge_repository(conn: psycopg.Connection, repository_id: str, lineage_ids: list[str]) -> dict[str, Any]:
    with conn.transaction():
        return conn.execute("SELECT rkp_purge_repository(%s, %s)", (repository_id, sorted(lineage_ids))).fetchone()[0]


def purge_tenant(conn: psycopg.Connection) -> int:
    with conn.transaction():
        return conn.execute("SELECT rkp_purge_tenant()").fetchone()[0]


def snapshot_rows(conn: psycopg.Connection, tenant_id: str, repository_id: str | None = None) -> list[dict[str, Any]]:
    q = "SELECT rkp_hash, repository_id, commit_sha, status, extract(epoch FROM published_at) FROM snapshots WHERE tenant_id=%s"
    p: list[Any] = [tenant_id]
    if repository_id is not None:
        q += " AND repository_id=%s"; p.append(repository_id)
    return [{"rkp_hash": h, "repository_id": r, "commit_sha": c, "status": st, "published_at": float(at)} for h, r, c, st, at in conn.execute(q + " ORDER BY published_at, rkp_hash", p)]


def audit(conn: psycopg.Connection, tenant_id: str, actor: str, action: str, subject_hash: str, details: dict[str, Any]) -> None:
    """Record an action. `details` must hold hashes and counts only, never content."""
    import uuid
    with conn.transaction():
        conn.execute("INSERT INTO audit_events (tenant_id, event_id, actor, action, subject_hash, details) VALUES (%s,%s,%s,%s,%s,%s)",
                     (tenant_id, uuid.uuid4().hex, actor, action, subject_hash, Jsonb(details)))
