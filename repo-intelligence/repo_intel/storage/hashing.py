"""Canonical serialization, content IDs and Merkle digests (spec §4.1, §4.3).

Everything that is hashed goes through RFC 8785 canonical JSON, so a digest
depends on logical content only, never on key order, row order or file bytes.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

import rfc8785

ID_LENGTH = 26  # base32 characters, 130 bits


def canonical(value: Any) -> bytes:
    """RFC 8785 canonical JSON bytes for a JSON-compatible value."""
    return rfc8785.dumps(value)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    """`sha256:<hex>` over the canonical form of a JSON-compatible value."""
    return "sha256:" + sha256_hex(canonical(value))


def digest_bytes(data: bytes) -> str:
    return "sha256:" + sha256_hex(data)


def make_id(prefix: str, *parts: Any) -> str:
    """Deterministic prefixed ID, e.g. `ent_…`, derived from content alone."""
    raw = hashlib.sha256(canonical(list(parts))).digest()
    return f"{prefix}_{base64.b32encode(raw).decode().lower()[:ID_LENGTH]}"


def rows_digest(rows: Iterable[Mapping[str, Any]], id_field: str = "id") -> str:
    """Digest of a set of rows, independent of the order they arrive in."""
    ordered = sorted(rows, key=lambda r: r[id_field])
    h = hashlib.sha256()
    for row in ordered:
        h.update(canonical(row))
        h.update(b"\n")
    return "sha256:" + h.hexdigest()


def merkle_root(leaves: Mapping[str, str]) -> str:
    """Root over named leaf digests (shard name -> shard digest)."""
    return digest([[name, leaves[name]] for name in sorted(leaves)])
