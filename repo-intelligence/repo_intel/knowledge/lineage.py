"""Lineage reconciliation for every entity kind (spec §4.2, GI-020).

Snapshot identity (`ent_…`) changes when a thing moves. Lineage identity
follows it across revisions. Reconciliation runs after publish, compares the
new snapshot with the previous one for the same ref, and links each added
entity to a removed one when one of two bases holds:

- `shared_evidence`: same kind and name, and they share content-derived
  evidence (an unchanged file that moved keeps its evidence IDs);
- `git_rename`: Git's rename detection (`git diff -M`) says the file, or the
  directory holding it, moved, and rewriting the old natural key's paths gives
  exactly the new natural key. This also covers a file that moved and changed.

Matching names alone never establish lineage. A link carries its basis and
lives only in `entity_lineage`, never in a snapshot or its hashes. A row is
written where an entity's lineage differs from its own ID.
"""

from __future__ import annotations

import posixpath
import subprocess
from pathlib import Path
from typing import Any

import psycopg

from repo_intel.compile.contracts import Manifest
from repo_intel.storage.hashing import digest
from repo_intel.storage.store import ObjectStore

PATH_FIELDS = ("path", "manifest_dir", "root", "component_root")
RENAME_INDEX = "renames"


# -- Git rename detection


def git_renames(repo_path: str | Path, old_sha: str, new_sha: str, threshold: int = 50) -> dict[str, tuple[str, int]]:
    """old path -> (new path, similarity %), from `git diff -M` between two commits.

    Works in a bare mirror as long as both commits' trees are present. Returns
    {} when Git cannot compare them (for example a commit missing from a
    shallow mirror), so rename detection is an aid and never a failure.
    """
    try:
        out = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo_path), "diff", "--no-ext-diff", "--no-textconv",
             "--name-status", f"-M{threshold}%", "-z", old_sha, new_sha, "--"],
            check=True, capture_output=True, timeout=120,
        ).stdout.decode("utf-8", "surrogateescape")
    except (OSError, subprocess.SubprocessError):
        return {}
    parts = out.split("\0")
    found: dict[str, tuple[str, int]] = {}
    i = 0
    while i < len(parts) and parts[i]:
        status = parts[i]
        if status.startswith("R") and i + 2 < len(parts):
            found[parts[i + 1]] = (parts[i + 2], int(status[1:] or 100))
            i += 3
        elif status.startswith("C") and i + 2 < len(parts):
            i += 3  # a copy is not a move
        else:
            i += 2
    return found


def directory_moves(renames: dict[str, tuple[str, int]]) -> dict[str, str]:
    """Directory prefix moves implied by file renames: `packages/ui` -> `packages/design`.

    A directory counts as moved only when every renamed file under it lands
    under the same new directory with the same relative path.
    """
    votes: dict[str, set[str]] = {}
    for old, (new, _) in renames.items():
        a, b = old.split("/"), new.split("/")
        k = 0
        while k < min(len(a), len(b)) and a[len(a) - 1 - k] == b[len(b) - 1 - k]:
            k += 1
        old_dir, new_dir = "/".join(a[: len(a) - k]), "/".join(b[: len(b) - k])
        if k and old_dir != new_dir:
            votes.setdefault(old_dir, set()).add(new_dir)
    return {old: next(iter(new)) for old, new in votes.items() if len(new) == 1 and old}


def _rewrite(value: str, files: dict[str, tuple[str, int]], dirs: dict[str, str]) -> str:
    if value in files:
        return files[value][0]
    for old in sorted(dirs, key=len, reverse=True):
        if value == old or value.startswith(old + "/"):
            return (dirs[old] + value[len(old):]).lstrip("/")
    return value


def store_renames(store: ObjectStore, a: Manifest, b: Manifest, renames: dict[str, tuple[str, int]]) -> None:
    """Keep the rename map next to the path manifests, so a delta can use it without the mirror."""
    value = {"from": a.source_tree_hash, "to": b.source_tree_hash, "renames": {k: list(v) for k, v in sorted(renames.items())}}
    store.index_put(RENAME_INDEX, rename_key(a.source_tree_hash, b.source_tree_hash), store.put_json(value))


def rename_key(from_tree: str, to_tree: str) -> str:
    return digest([from_tree, to_tree])


def load_renames(store: ObjectStore, a: Manifest, b: Manifest) -> dict[str, tuple[str, int]] | None:
    d = store.index_get(RENAME_INDEX, rename_key(a.source_tree_hash, b.source_tree_hash))
    if d is None or not store.has(d):
        return None
    return {k: (v[0], v[1]) for k, v in store.get_json(d)["renames"].items()}


def rename_moves(removed: dict[str, dict], added: dict[str, dict], renames: dict[str, tuple[str, int]], known: dict[str, str] | None = None) -> dict[str, tuple[str, str]]:
    """old id -> (new id, basis) for entities whose natural key, with paths rewritten by Git's renames, is exactly the new one."""
    if not renames:
        return {}
    dirs = directory_moves(renames)
    by_key: dict[tuple[str, str], list[str]] = {}
    for eid, e in added.items():
        by_key.setdefault((e["kind"], digest(e["natural_key"])), []).append(eid)
    found: dict[str, tuple[str, str]] = {}
    for eid, e in removed.items():
        key = dict(e["natural_key"])
        touched = None
        for field in PATH_FIELDS:
            value = key.get(field)
            if isinstance(value, str) and value:
                new = _rewrite(value, renames, dirs)
                if new != value:
                    key[field] = new
                    touched = renames.get(value, (None, None))[1]
        if touched is None and not any(isinstance(key.get(f), str) and key.get(f) for f in PATH_FIELDS):
            continue
        if key == e["natural_key"]:
            continue
        targets = by_key.get((e["kind"], digest(key)), [])
        if len(targets) == 1:
            basis = f"git rename detection ({touched}% similar)" if touched is not None else "git rename detection (directory moved)"
            found[eid] = (targets[0], basis)
    claimed: dict[str, list[str]] = {}
    for old, (new, _) in found.items():
        claimed.setdefault(new, []).append(old)
    return {old: v for old, v in found.items() if len(claimed[v[0]]) == 1 and (known is None or old not in known)}


# -- persisted lineage


def identities(conn: psycopg.Connection, tenant: str, rkp_hash: str) -> dict[str, str]:
    """entity ID -> lineage ID for the entities of one snapshot that are not their own lineage."""
    return dict(conn.execute("SELECT entity_id, lineage_id FROM entity_lineage WHERE tenant_id=%s AND rkp_hash=%s", (tenant, rkp_hash)))


def _rows(conn: psycopg.Connection, tenant: str, rkp_hash: str) -> dict[str, tuple[str, str | None, str]]:
    return {e: (l, p, b) for e, l, p, b in conn.execute(
        "SELECT entity_id, lineage_id, previous_id, basis FROM entity_lineage WHERE tenant_id=%s AND rkp_hash=%s", (tenant, rkp_hash))}


def reconcile(conn: psycopg.Connection, tenant: str, store: ObjectStore, published: Manifest, previous: Manifest | None, renames: dict[str, tuple[str, int]] | None = None) -> dict[str, tuple[str, str]]:
    """Link the published snapshot's entities to the previous snapshot's lineage. Returns the moves found."""
    from repo_intel.knowledge.delta import _graph, match_moves

    if previous is None or previous.rkp_hash == published.rkp_hash or previous.repository_id != published.repository_id:
        return {}
    current, _ = _graph(store, published)
    baseline, _ = _graph(store, previous)
    carried = _rows(conn, tenant, previous.rkp_hash)
    removed = {k: v for k, v in baseline.items() if k not in current}
    added = {k: v for k, v in current.items() if k not in baseline}
    moves = match_moves(removed, added, renames)
    rows: dict[str, tuple[str, str | None, str]] = {k: (carried[k][0], None, "carried") for k in current if k in carried}
    for old, (new, basis) in moves.items():
        lineage = carried[old][0] if old in carried else old
        rows[new] = (lineage, old, basis)
    if rows:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO entity_lineage (tenant_id, repository_id, rkp_hash, entity_id, lineage_id, previous_id, basis)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                [(tenant, published.repository_id, published.rkp_hash, k, *rows[k]) for k in sorted(rows)],
            )
    return moves


def history(conn: psycopg.Connection, tenant: str, entity_id: str, limit: int = 200) -> dict[str, Any] | None:
    """Cross-revision history of the logical entity that `entity_id` belongs to."""
    row = conn.execute("SELECT lineage_id FROM entity_lineage WHERE tenant_id=%s AND entity_id=%s LIMIT 1", (tenant, entity_id)).fetchone()
    lineage = row[0] if row else entity_id
    links = {(e, h): (p, b) for e, p, b, h in conn.execute(
        "SELECT entity_id, previous_id, basis, rkp_hash FROM entity_lineage WHERE tenant_id=%s AND lineage_id=%s", (tenant, lineage))}
    members = sorted({lineage, entity_id, *(e for e, _ in links)})
    revisions = conn.execute(
        """SELECT DISTINCT ON (s.rkp_hash, e.entity_id) s.rkp_hash, s.commit_sha, s.repository_id, s.status, s.published_at, e.entity_id, e.kind, e.name, e.row->'natural_key'
           FROM entities e JOIN snapshot_shards ss ON ss.tenant_id=e.tenant_id AND ss.shard_digest=e.shard_digest
           JOIN snapshots s ON s.tenant_id=ss.tenant_id AND s.rkp_hash=ss.rkp_hash
           WHERE e.tenant_id=%s AND e.entity_id = ANY(%s) AND s.status <> 'retracted'
           ORDER BY s.rkp_hash, e.entity_id""",
        (tenant, members),
    ).fetchall()
    if not revisions:
        return None
    entries = []
    for h, sha, repo, status, at, eid, kind, name, key in sorted(revisions, key=lambda r: (r[4], r[0])):
        link = links.get((eid, h))
        entries.append({
            "rkp_hash": h, "commit_sha": sha, "repository_id": repo, "status": status, "published_at": at.isoformat(),
            "entity_id": eid, "kind": kind, "name": name, "natural_key": key,
            "moved_from": link[0] if link else None, "basis": link[1] if link else "identity",
        })
    return {"lineage_id": lineage, "entity_id": entity_id, "entity_ids": members, "revisions": entries[:limit], "truncated": len(entries) > limit}
