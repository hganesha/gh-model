"""Tenant repository metadata and an idempotent import of the local JSON registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb



def import_file(conn: psycopg.Connection, tenant: str, path: Path) -> None:
    if not path.is_file():
        return
    repositories = json.loads(path.read_text(encoding="utf-8"))["repositories"]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO repository_registry VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            [(tenant, repo_id, Jsonb(row)) for repo_id, row in repositories.items()],
        )


def read(conn: psycopg.Connection, tenant: str, repository_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT document FROM repository_registry WHERE tenant_id=%s AND repository_id=%s",
        (tenant, repository_id),
    ).fetchone()
    return row[0] if row else None


def all_repositories(conn: psycopg.Connection, tenant: str) -> list[dict[str, Any]]:
    return [row for (row,) in conn.execute(
        "SELECT document FROM repository_registry WHERE tenant_id=%s ORDER BY document->>'full_name', repository_id",
        (tenant,),
    )]


def update(conn: psycopg.Connection, tenant: str, repository_id: str, **fields: Any) -> dict[str, Any]:
    entry = {"repository_id": repository_id, "snapshots": []}
    with conn.transaction():
        conn.execute("INSERT INTO repository_registry VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (tenant, repository_id, Jsonb(entry)))
        row, = conn.execute(
            "SELECT document FROM repository_registry WHERE tenant_id=%s AND repository_id=%s FOR UPDATE",
            (tenant, repository_id),
        ).fetchone()
        snapshot = fields.pop("snapshot", None)
        row.update(fields)
        if snapshot:
            row["snapshots"] = [s for s in row["snapshots"] if s["rkp_hash"] != snapshot["rkp_hash"]] + [snapshot]
        conn.execute(
            "UPDATE repository_registry SET document=%s WHERE tenant_id=%s AND repository_id=%s",
            (Jsonb(row), tenant, repository_id),
        )
    return row


def forget_snapshots(conn: psycopg.Connection, tenant: str, repository_id: str, rkp_hashes: set[str]) -> None:
    """Drop snapshot entries from a repository's registry document (retention)."""
    with conn.transaction():
        found = conn.execute(
            "SELECT document FROM repository_registry WHERE tenant_id=%s AND repository_id=%s FOR UPDATE",
            (tenant, repository_id),
        ).fetchone()
        if not found:
            return
        row = found[0]
        kept = [s for s in row.get("snapshots", []) if s["rkp_hash"] not in rkp_hashes]
        if len(kept) != len(row.get("snapshots", [])):
            conn.execute("UPDATE repository_registry SET document=%s WHERE tenant_id=%s AND repository_id=%s",
                         (Jsonb(row | {"snapshots": kept}), tenant, repository_id))


# -- tracked refs (spec §4.4, §4.5)

TRACKED_COLUMNS = "repository_id, ref, kind, head_sha, extract(epoch FROM head_seen_at), rkp_hash, published_sha, extract(epoch FROM published_at), extract(epoch FROM closed_at), extract(epoch FROM compile_after), extract(epoch FROM requested_at)"
_FIELDS = ("repository_id", "ref", "kind", "head_sha", "head_seen_at", "rkp_hash", "published_sha", "published_at", "closed_at", "compile_after", "requested_at")


def ref_kind(ref: str) -> str:
    if ref.startswith("refs/tags/"):
        return "tag"
    if ref.startswith("refs/pull/") or ref.startswith("refs/merge-requests/"):
        return "pull_request"
    return "branch"


def _ref_row(values: tuple) -> dict[str, Any]:
    row = {k: (float(v) if k.endswith("_at") or k == "compile_after" else v) if v is not None else None for k, v in zip(_FIELDS, values)}
    row["state"] = "current" if row["rkp_hash"] and row["published_sha"] == row["head_sha"] else "stale"
    return row


def track_head(conn: psycopg.Connection, tenant: str, repository_id: str, ref: str, sha: str, *, compile_after: float | None = None, reopen: bool = True) -> dict[str, Any]:
    """Record a ref's newest head. The ref view turns `stale` until a snapshot of that head publishes."""
    with conn.transaction():
        values = conn.execute(
            f"""INSERT INTO tracked_refs (tenant_id, repository_id, ref, kind, head_sha, head_seen_at, compile_after)
                VALUES (%s,%s,%s,%s,%s,clock_timestamp(),to_timestamp(%s))
                ON CONFLICT (tenant_id, repository_id, ref) DO UPDATE SET
                  head_sha=excluded.head_sha, head_seen_at=excluded.head_seen_at,
                  compile_after=COALESCE(excluded.compile_after, tracked_refs.compile_after),
                  closed_at=CASE WHEN %s THEN NULL ELSE tracked_refs.closed_at END
                RETURNING {TRACKED_COLUMNS}""",
            (tenant, repository_id, ref, ref_kind(ref), sha, compile_after, reopen),
        ).fetchone()
    return _ref_row(values)


def ref_requested(conn: psycopg.Connection, tenant: str, repository_id: str, ref: str) -> None:
    with conn.transaction():
        conn.execute("UPDATE tracked_refs SET requested_at=clock_timestamp() WHERE tenant_id=%s AND repository_id=%s AND ref=%s", (tenant, repository_id, ref))


def ref_published(conn: psycopg.Connection, tenant: str, repository_id: str, ref: str, sha: str, rkp_hash: str) -> None:
    with conn.transaction():
        conn.execute(
            """INSERT INTO tracked_refs (tenant_id, repository_id, ref, kind, head_sha, head_seen_at, rkp_hash, published_sha, published_at)
               VALUES (%s,%s,%s,%s,%s,clock_timestamp(),%s,%s,clock_timestamp())
               ON CONFLICT (tenant_id, repository_id, ref) DO UPDATE SET
                 rkp_hash=excluded.rkp_hash, published_sha=excluded.published_sha, published_at=excluded.published_at""",
            (tenant, repository_id, ref, ref_kind(ref), sha, rkp_hash, sha),
        )


def close_ref(conn: psycopg.Connection, tenant: str, repository_id: str, ref: str) -> None:
    with conn.transaction():
        conn.execute("UPDATE tracked_refs SET closed_at=clock_timestamp(), compile_after=NULL WHERE tenant_id=%s AND repository_id=%s AND ref=%s", (tenant, repository_id, ref))


def tracked_ref(conn: psycopg.Connection, tenant: str, repository_id: str, ref: str) -> dict[str, Any] | None:
    values = conn.execute(f"SELECT {TRACKED_COLUMNS} FROM tracked_refs WHERE tenant_id=%s AND repository_id=%s AND ref=%s", (tenant, repository_id, ref)).fetchone()
    return _ref_row(values) if values else None


def tracked_refs(conn: psycopg.Connection, tenant: str, repository_id: str | None = None) -> list[dict[str, Any]]:
    q = f"SELECT {TRACKED_COLUMNS} FROM tracked_refs WHERE tenant_id=%s"
    p: list[Any] = [tenant]
    if repository_id is not None:
        q += " AND repository_id=%s"; p.append(repository_id)
    return [_ref_row(v) for v in conn.execute(q + " ORDER BY repository_id, ref", p)]
