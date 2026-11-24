"""Rebuild one tenant's PostgreSQL projection from the object store (spec §12, GI-012).

    python -m repo_intel.knowledge.rebuild_projection --tenant T --store-root DIR --conninfo "host=… user=owner …" [--dry-run]

The shard store is the source of truth. This drops the tenant's projection
rows (snapshots, snapshot_shards, shards, entities, assertions, evidence) and
loads every manifest in the store's `rkp` index again. Snapshots that were
retracted are restored as tombstones with their retraction record; quarantine
marks are restored too. Registry, settings, refs, lineage and business-map
tables are not derived from shards and are left as they are. Run it as the
schema owner; it applies pending migrations first.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from repo_intel.storage import gc
from repo_intel.knowledge import projection
from repo_intel.compile.contracts import Manifest
from repo_intel.storage.store import ObjectStore

QUARANTINE = "quarantine"


def manifests(store: ObjectStore) -> list[Manifest]:
    found = []
    for _, d, _ in store.list_index("rkp"):
        if store.has(d):
            found.append(Manifest.model_validate(store.get_json(d)))
    return sorted(found, key=lambda m: m.rkp_hash)


def rebuild(conn: psycopg.Connection, store: ObjectStore) -> dict[str, Any]:
    """Replace the tenant's projection rows with what the store holds. Run as the owner."""
    t = store.tenant_id
    projection.ensure_schema(conn)
    tombstoned = gc.tombstones(store)
    quarantined = {key.replace("_", ":", 1) for key, _, _ in store.list_index(QUARANTINE)}
    loaded = 0
    with conn.transaction():
        conn.execute("SELECT set_config(%s, %s, true)", (projection._TENANT_SETTING, t))
        for table in ("snapshot_shards", "entities", "assertions", "evidence", "shards", "snapshots"):
            conn.execute(f"DELETE FROM {table} WHERE tenant_id=%s", (t,))
    for m in manifests(store):
        if m.rkp_hash in tombstoned:
            with conn.transaction():
                conn.execute("INSERT INTO snapshots (tenant_id, rkp_hash, repository_id, commit_sha, status, manifest, retraction) VALUES (%s,%s,%s,%s,'retracted',%s,%s) ON CONFLICT DO NOTHING",
                             (t, m.rkp_hash, m.repository_id, m.commit_sha, Jsonb(m.row()), Jsonb(tombstoned[m.rkp_hash])))
            continue
        if not all(store.has(s.digest) for s in m.shards):
            continue  # collected content: nothing to project
        projection.project_snapshot(conn, store, m)
        if m.rkp_hash in quarantined:
            conn.execute("UPDATE snapshots SET status='quarantined' WHERE tenant_id=%s AND rkp_hash=%s", (t, m.rkp_hash))
            conn.commit()
        loaded += 1
    return {"tenant": t, "snapshots": loaded, "tombstones": len(tombstoned)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--store-root", required=True)
    ap.add_argument("--conninfo", required=True, help="owner connection string")
    ap.add_argument("--dry-run", action="store_true", help="list what would be loaded and change nothing")
    args = ap.parse_args(argv)
    store = ObjectStore(args.store_root, args.tenant)
    if args.dry_run:
        found = manifests(store)
        print(f"{len(found)} snapshot(s) in the store for tenant {args.tenant}")
        return 0
    with psycopg.connect(args.conninfo) as conn:
        print(rebuild(conn, store))
    return 0


if __name__ == "__main__":
    sys.exit(main())
