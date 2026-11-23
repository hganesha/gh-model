"""Source pinning: build the path manifest for one commit (spec §5, Stage 0).

Reads the Git object database directly. Nothing is checked out, no hook runs,
symlinks and submodules are recorded and never followed, and every exclusion
is listed with a reason.
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
from pathlib import Path
from typing import Any

from . import secrets
from .hashing import digest, sha256_hex
from .store import ObjectStore

MAX_PARSE_BYTES = 1_000_000
EXCLUDED_DIRS = {"node_modules", "vendor", "dist", "build", ".next", "__pycache__", ".venv", "target"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".mp4", ".pdf", ".zip", ".gz", ".jar", ".so", ".dylib", ".exe"}
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")

_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "PATH": os.environ.get("PATH", "")}


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    cmd = ["git", "-C", str(repo), "-c", "core.hooksPath=" + os.devnull, *args]
    return subprocess.run(cmd, env=_ENV, input=stdin, check=True, capture_output=True).stdout


def _exclusion_reason(path: str, mode: str, obj_type: str) -> str | None:
    if obj_type == "commit":
        return "submodule"
    if mode == "120000":
        return "symlink"
    if any(part in EXCLUDED_DIRS for part in path.split("/")[:-1]):
        return "vendored_or_generated_dir"
    if posixpath.splitext(path)[1].lower() in BINARY_EXT:
        return "binary"
    return None


def _read_blobs(repo: Path, oids: list[str]) -> dict[str, bytes]:
    if not oids:
        return {}
    out = _git(repo, "cat-file", "--batch", stdin=("\n".join(oids) + "\n").encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in oids:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].split()
        if len(header) != 3 or header[1] != b"blob":
            raise RuntimeError(f"unexpected object for {oid}: {out[pos:nl]!r}")
        size = int(header[2])
        blobs[oid] = out[nl + 1 : nl + 1 + size]
        pos = nl + 1 + size + 1
    return blobs


def snapshot_commit(store: ObjectStore, repo_path: str | os.PathLike[str], commit_sha: str) -> dict[str, Any]:
    """Return the path manifest for `commit_sha` and store its parseable blobs.

    Manifest: {commit_sha, entries: [{path, blob_sha256, git_oid, size, mode, parse, secret_findings?}],
               exclusions: [{path, reason, git_oid, detail?}], source_tree_hash}
    """
    if not _SHA.match(commit_sha):
        raise ValueError("commit_sha must be a full lowercase hex SHA; resolve refs before compiling")
    repo = Path(repo_path)
    _git(repo, "rev-parse", "--verify", "--quiet", f"{commit_sha}^{{commit}}")

    listing = _git(repo, "ls-tree", "-r", "-z", "--long", commit_sha)
    tree = []
    for record in filter(None, listing.split(b"\0")):
        meta, path = record.split(b"\t", 1)
        mode, obj_type, oid, size = meta.decode().split()
        tree.append((path.decode("utf-8", "surrogateescape"), mode, obj_type, oid, size))
    tree.sort()

    entries: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    wanted = [oid for path, mode, typ, oid, _ in tree if _exclusion_reason(path, mode, typ) is None]
    link_oids = [oid for _, mode, typ, oid, _ in tree if mode == "120000" and typ == "blob"]
    blobs = _read_blobs(repo, sorted(set(wanted + link_oids)))

    for path, mode, obj_type, oid, size in tree:
        reason = _exclusion_reason(path, mode, obj_type)
        if reason:
            item: dict[str, Any] = {"path": path, "reason": reason, "git_oid": oid}
            if reason == "symlink":
                target = blobs[oid].decode("utf-8", "replace")
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
                item["detail"] = {"escapes_root": target.startswith("/") or resolved.startswith("..")}
            exclusions.append(item)
            continue
        data = blobs[oid]
        blob_sha = sha256_hex(data)
        store.put_bytes(data)
        entry = {"path": path, "blob_sha256": blob_sha, "git_oid": oid, "size": int(size), "mode": mode, "parse": int(size) <= MAX_PARSE_BYTES}
        findings = secrets.scan(data)
        if findings:  # location and detector only; the value is never recorded
            entry["secret_findings"] = findings
        entries.append(entry)

    body = {"entries": entries, "exclusions": exclusions}
    return {"commit_sha": commit_sha, **body, "source_tree_hash": digest(body)}
