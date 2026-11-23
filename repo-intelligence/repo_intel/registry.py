"""Tenant repository metadata and an idempotent import of the local JSON registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

TABLES = {
    "repository_registry": "SELECT, INSERT, UPDATE",
    "connection_checks": "SELECT, INSERT, UPDATE",
    "webhook_deliveries": "SELECT, INSERT, DELETE",
}
DDL = """
CREATE TABLE IF NOT EXISTS repository_registry (
  tenant_id text NOT NULL, repository_id text NOT NULL, document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, repository_id));
CREATE TABLE IF NOT EXISTS connection_checks (
  tenant_id text NOT NULL, connection_id text NOT NULL, document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, connection_id));
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  tenant_id text NOT NULL, connection_id text NOT NULL, delivery_id text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, connection_id, delivery_id));
"""


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
