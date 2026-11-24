"""Retention, mark-and-sweep garbage collection and deletion (spec §4.5, §12; GI-021, GI-006).

Retention decides which snapshots are kept (`retained`). Collection then:

1. **Marks** every object reachable from the kept manifests (shards, coverage,
   conflicts, unknowns, ledger entries, toolchain and catalogue, workspace
   exports, path manifests), plus tombstoned manifests, plus the values of
   cache index entries used within `step_cache_days`. Reachability follows
   every `sha256:` reference inside a JSON object, so a cached step output
   that points at another object keeps it.
2. **Sweeps** index entries whose target is no longer kept, then objects that
   are unmarked and older than a grace period (an in-flight compile may have
   written them a moment ago). Source blobs are a cache: one not used within
   `blob_cache_days` is evicted whether marked or not; it is refilled from the
   host by OID when a compile needs it again.
3. **Drops** projection rows of snapshots that are no longer kept, with any
   shard rows no remaining snapshot uses, and forgets them in the registry.
4. **Removes** bare mirrors not fetched into within `mirror_days`.

`purge_repository` runs the same collection with the repository's snapshots
removed from the roots and with caches reset to what kept manifests reach, so
no step output, ledger entry, placement attempt or blob that only the deleted
repository produced survives. `purge_tenant` removes the tenant's whole tree.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import Manifest
from .settings import RetentionPolicy
from .store import ObjectStore

DAY = 86400.0
_REF = re.compile(rb"sha256:[0-9a-f]{64}")
CACHE_NAMESPACES = ("step_cache", "ledger", "placement_attempts", "embeddings")
SNAPSHOT_NAMESPACES = ("rkp", "snapshots", "latest_snapshot", "workspace_exports", "path_manifests", "renames")
TOMBSTONES = "retractions"


@dataclass
class Report:
    retained: list[str] = field(default_factory=list)
    dropped_snapshots: list[str] = field(default_factory=list)
    objects_deleted: int = 0
    blobs_evicted: int = 0
    index_entries_deleted: int = 0
    mirrors_deleted: int = 0
    projection_snapshots_deleted: int = 0

    def row(self) -> dict[str, Any]:
        return {k: (len(v) if isinstance(v, list) else v) for k, v in self.__dict__.items()} | {"retained_hashes": sorted(self.retained), "dropped_hashes": sorted(self.dropped_snapshots)}


# -- retention


def retained(repositories: list[dict[str, Any]], refs: list[dict[str, Any]], snapshots: list[dict[str, Any]], policy: RetentionPolicy, now: float) -> set[str]:
    """rkp_hashes to keep, from the registry's snapshot entries, tracked refs and projected snapshot rows.

    - each tracked ref's current snapshot (a pull request's only until
      `pull_request_days_after_close` after it closed);
    - branch snapshots compiled within `branch_days`;
    - tag snapshots for `tag_days`, or while the tag is tracked;
    - pull request snapshots while the request is open, and for
      `pull_request_days_after_close` after it closed;
    - anything published within the last day that the registry does not know
      yet (a compile between publish and registration).
    Retracted snapshots keep their tombstone but never their content.
    """
    from .registry import ref_kind

    status = {s["rkp_hash"]: s["status"] for s in snapshots}
    closed = {(r["repository_id"], r["ref"]): r["closed_at"] for r in refs if r["kind"] == "pull_request"}
    keep: set[str] = set()
    for r in refs:
        if not r["rkp_hash"]:
            continue
        if r["kind"] == "pull_request" and r["closed_at"] and now - r["closed_at"] > policy.pull_request_days_after_close * DAY:
            continue
        keep.add(r["rkp_hash"])
    known: set[str] = set()
    for repo in repositories:
        for snap in repo.get("snapshots", []):
            known.add(snap["rkp_hash"])
            age = now - float(snap.get("compiled_at") or 0)
            kind = ref_kind(snap["ref"])
            if kind == "tag":
                ok = policy.tag_days is None or age <= policy.tag_days * DAY
            elif kind == "pull_request":
                closed_at = closed.get((repo["repository_id"], snap["ref"]))
                ok = closed_at is None or now - closed_at <= policy.pull_request_days_after_close * DAY
            else:
                ok = age <= policy.branch_days * DAY
            if ok:
                keep.add(snap["rkp_hash"])
    for s in snapshots:
        if s["rkp_hash"] not in known and now - s["published_at"] < DAY:
            keep.add(s["rkp_hash"])
    return {h for h in keep if status.get(h) != "retracted"}


# -- mark and sweep over the object store


def _closure(store: ObjectStore, roots: set[str], blobs: set[str]) -> set[str]:
    """Every object reachable from `roots` through `sha256:` references. Blobs are leaves."""
    marked: set[str] = set()
    todo = [d for d in roots if d]
    while todo:
        d = todo.pop()
        if d in marked:
            continue
        marked.add(d)
        if d in blobs or not store.has(d):
            continue
        try:
            data = store.get_bytes(d)
        except FileNotFoundError:
            continue
        if data[:1] not in (b"{", b"["):
            continue
        todo.extend(m.decode() for m in _REF.findall(data) if m.decode() not in marked)
    return marked


def _manifest_roots(store: ObjectStore, rkp_hash: str) -> tuple[set[str], Manifest | None]:
    d = store.index_get("rkp", rkp_hash)
    if d is None or not store.has(d):
        return set(), None
    m = Manifest.model_validate(store.get_json(d))
    roots = {d, m.toolchain_hash}
    for value in (store.index_get("workspace_exports", rkp_hash), store.index_get("path_manifests", m.source_tree_hash)):
        if value:
            roots.add(value)
    return roots, m


def collect_store(store: ObjectStore, keep: set[str], policy: RetentionPolicy, now: float | None = None, *, grace_seconds: float = 3600.0, strict: bool = False, mirrors: Path | None = None) -> Report:
    """Mark from the kept snapshots and sweep everything else. See the module docstring."""
    now = time.time() if now is None else now
    report = Report(retained=sorted(keep))
    roots: set[str] = set()
    kept_manifests: dict[str, Manifest] = {}
    for h in keep:
        r, m = _manifest_roots(store, h)
        roots |= r
        if m:
            kept_manifests[h] = m
    kept_trees = {m.source_tree_hash for m in kept_manifests.values()}
    tombstoned = {key for key, _, _ in store.list_index(TOMBSTONES)}
    leaves: set[str] = set()  # kept themselves, but what they reference is not
    for key, value, _ in store.list_index(TOMBSTONES):
        roots.add(value)
        d = store.index_get("rkp", key)
        if d:
            leaves.add(d)  # the manifest (hashes only) stays readable for the pointer to the replacement

    # Blobs named by any surviving path manifest are cache entries with a TTL.
    blobs: set[str] = set()
    for tree in kept_trees:
        d = store.index_get("path_manifests", tree)
        if d and store.has(d):
            blobs.update("sha256:" + e["blob_sha256"] for e in store.get_json(d)["entries"])

    # Live cache entries are roots unless this is a purge, which resets caches
    # to what kept manifests reach.
    cache_cutoff = now - policy.step_cache_days * DAY
    for ns in store.namespaces():
        if ns in SNAPSHOT_NAMESPACES or ns == TOMBSTONES:
            continue
        for _, value, used in store.list_index(ns):
            if ns in CACHE_NAMESPACES and (strict or used < cache_cutoff):
                continue
            roots.add(value)
    for _, value, _ in store.list_index("renames"):
        if store.has(value):
            pair = store.get_json(value)
            if pair["from"] in kept_trees and pair["to"] in kept_trees:
                roots.add(value)

    marked = _closure(store, roots, blobs) | leaves

    # Index entries: snapshot pointers follow their snapshot; caches follow use.
    for ns in store.namespaces():
        for key, value, used in list(store.list_index(ns)):
            if ns == TOMBSTONES:
                continue
            if ns in SNAPSHOT_NAMESPACES:
                drop = value not in marked and not (ns == "rkp" and key in tombstoned)
            elif ns in CACHE_NAMESPACES:
                drop = value not in marked or (not strict and used < cache_cutoff)
            else:
                drop = False
            if drop and store.index_delete(ns, key):
                report.index_entries_deleted += 1

    # Objects.
    blob_cutoff = now - policy.blob_cache_days * DAY
    for d, used in list(store.list_objects()):
        if d in blobs:
            if used < blob_cutoff and store.delete(d):
                report.blobs_evicted += 1
        elif d not in marked and (strict or used < now - grace_seconds):
            if store.delete(d):
                report.objects_deleted += 1

    if mirrors is not None and mirrors.exists():
        for mirror in mirrors.iterdir():
            if mirror.is_dir() and mirror.stat().st_mtime < now - policy.mirror_days * DAY:
                shutil.rmtree(mirror, ignore_errors=True)
                report.mirrors_deleted += 1
    return report


def mirror_paths(mirrors: Path, repository_id: str) -> list[Path]:
    """Bare mirrors a connector may have created for a repository."""
    import hashlib
    names = {repository_id.replace("/", "_") + ".git", hashlib.sha256(repository_id.encode()).hexdigest() + ".git"}
    return [mirrors / n for n in sorted(names) if (mirrors / n).exists()]


def tombstone(store: ObjectStore, rkp_hash: str, record: dict[str, Any]) -> None:
    store.index_put(TOMBSTONES, rkp_hash, store.put_json(record))


def tombstones(store: ObjectStore) -> dict[str, dict[str, Any]]:
    return {key.replace("_", ":", 1): store.get_json(value) for key, value, _ in store.list_index(TOMBSTONES) if store.has(value)}
