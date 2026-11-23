"""Content-addressed object store, per tenant (spec §11, §12).

M0 uses the local filesystem. The interface is what an S3-backed store will
implement later. Objects are never shared between tenants.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .hashing import canonical, digest_bytes

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


class ObjectStore:
    def __init__(self, root: str | os.PathLike[str], tenant_id: str):
        if not _SAFE.match(tenant_id):
            raise ValueError("unsafe tenant id")
        self.tenant_id = tenant_id
        self._root = Path(root) / tenant_id
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
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_bytes(data)
            tmp.replace(path)
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
        return path.read_text() if path.exists() else None

    def index_put(self, namespace: str, key: str, value: str) -> None:
        path = self._index_path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(value)
        tmp.replace(path)
