"""Content-addressed object store, per tenant (spec §11, §12).

The store is a filesystem tree (locally, or an Azure Files share when hosted).
The interface is what an S3-backed store will implement later. Objects are
never shared between tenants.

Last use is the file's modification time. Writing an object that already
exists refreshes it, so a blob re-admitted by a compile stays in the cache;
reading a cache index entry (`TOUCHED_NAMESPACES`) refreshes that entry. Garbage
collection (`gc.py`) lists and deletes through the methods below; `sweep` is
the TTL eviction it builds on.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections.abc import Container, Iterator
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from repo_intel.storage.hashing import canonical, digest_bytes

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
# Cache namespaces whose entries record their last use when read.
TOUCHED_NAMESPACES = frozenset({"step_cache", "ledger", "embeddings", "placement_attempts"})


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = NamedTemporaryFile(dir=path.parent, delete=False)
    temporary_path = Path(temp.name)
    try:
        with temp:
            temp.write(data)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _touch(path: Path) -> None:
    try:
        now = time.time()
        os.utime(path, (now, now))
    except FileNotFoundError:
        pass


class ObjectStore:
    def __init__(self, root: str | os.PathLike[str], tenant_id: str):
        if not _SAFE.match(tenant_id):
            raise ValueError("unsafe tenant id")
        self.tenant_id = tenant_id
        self.root = Path(root) / tenant_id
        self._root = self.root
        (self._root / "objects").mkdir(parents=True, exist_ok=True)
        (self._root / "index").mkdir(parents=True, exist_ok=True)

    # -- content-addressed objects

    def _object_path(self, d: str) -> Path:
        if not _DIGEST.match(d):
            raise ValueError(f"not a digest: {d!r}")
        h = d.split(":", 1)[1]
        return self._root / "objects" / h[:2] / h[2:]

    def put_bytes(self, data: bytes) -> str:
        d = digest_bytes(data)
        path = self._object_path(d)
        if not path.exists():
            _atomic_write(path, data)
        else:
            _touch(path)
        return d

    def get_bytes(self, d: str) -> bytes:
        return self._object_path(d).read_bytes()

    def has(self, d: str) -> bool:
        return self._object_path(d).exists()

    def put_json(self, value: Any) -> str:
        return self.put_bytes(canonical(value))

    def get_json(self, d: str) -> Any:
        return json.loads(self.get_bytes(d))

    # -- small named indexes (step cache, snapshot pointers)

    def _index_path(self, namespace: str, key: str) -> Path:
        if not _SAFE.match(namespace) or not _SAFE.match(key.replace(":", "_")):
            raise ValueError("unsafe index key")
        return self._root / "index" / namespace / key.replace(":", "_")

    def index_get(self, namespace: str, key: str) -> str | None:
        path = self._index_path(namespace, key)
        try:
            value = path.read_text()
        except FileNotFoundError:
            return None
        if namespace in TOUCHED_NAMESPACES:
            _touch(path)
        return value

    def index_put(self, namespace: str, key: str, value: str) -> None:
        _atomic_write(self._index_path(namespace, key), value.encode())

    # -- listing, deletion and last use (garbage collection, cache TTL)

    def touch(self, d: str) -> None:
        """Record a use of an object now."""
        _touch(self._object_path(d))

    def last_used(self, d: str) -> float | None:
        try:
            return self._object_path(d).stat().st_mtime
        except FileNotFoundError:
            return None

    def list_objects(self) -> Iterator[tuple[str, float]]:
        """(digest, last use) for every object, in no particular order."""
        base = self._root / "objects"
        for prefix in sorted(base.iterdir()) if base.exists() else ():
            if not prefix.is_dir() or len(prefix.name) != 2:
                continue
            for item in prefix.iterdir():
                if item.name.startswith("tmp") or len(item.name) != 62:
                    continue  # an in-progress write
                try:
                    yield f"sha256:{prefix.name}{item.name}", item.stat().st_mtime
                except FileNotFoundError:
                    continue

    def delete(self, d: str) -> bool:
        path = self._object_path(d)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return True

    def sweep(self, cutoff: float, keep: Container[str] = (), only: Container[str] | None = None) -> list[str]:
        """Delete objects last used before `cutoff` that are not in `keep` (and, if given, are in `only`)."""
        gone = []
        for d, used in list(self.list_objects()):
            if used < cutoff and d not in keep and (only is None or d in only) and self.delete(d):
                gone.append(d)
        return gone

    def namespaces(self) -> list[str]:
        base = self._root / "index"
        return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.exists() else []

    def list_index(self, namespace: str) -> Iterator[tuple[str, str, float]]:
        """(stored key, value, last use) for every entry of a namespace. ':' in keys reads back as '_'."""
        if not _SAFE.match(namespace):
            raise ValueError("unsafe index namespace")
        base = self._root / "index" / namespace
        for item in sorted(base.iterdir()) if base.exists() else ():
            try:
                yield item.name, item.read_text(), item.stat().st_mtime
            except (FileNotFoundError, IsADirectoryError):
                continue

    def index_delete(self, namespace: str, key: str) -> bool:
        try:
            self._index_path(namespace, key).unlink()
            return True
        except FileNotFoundError:
            return False

    def destroy(self) -> None:
        """Remove everything this tenant holds: objects, indexes, mirrors and local files."""
        shutil.rmtree(self._root, ignore_errors=True)
