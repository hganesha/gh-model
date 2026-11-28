"""Source pinning: build the path manifest for one commit (spec §5, Stage 0).

Reads the Git object database directly. Nothing is checked out, no hook runs,
symlinks and submodules are recorded and never followed, and every exclusion
is listed with a reason. Nothing here touches the network: a mirror fetched as
a partial clone must already hold every blob `wanted_blobs` names.

The exclusion policy is part of the commit, so it is pinned and hashed with it:
built-in vendored/generated directories and binary extensions, then
`.gitattributes` `linguist-generated` / `linguist-vendored` (set or unset, as
GitHub reads them), then the repository's own `.rkp/exclude` globs. The files the
knowledge bundle writes under `.rkp/` (`repo_intel.knowledge.bundle`; see `in_bundle`)
are left out entirely, so committing the bundle does not change what it describes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import posixpath
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from repo_intel.ingest import secrets
from repo_intel.storage.hashing import digest, sha256_hex
from repo_intel.storage.store import ObjectStore

MAX_PARSE_BYTES = 1_000_000
EXCLUDED_DIRS = {"node_modules", "vendor", "dist", "build", ".next", "__pycache__", ".venv", "target"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".mp4", ".pdf", ".zip", ".gz", ".jar", ".so", ".dylib", ".exe"}
POLICY_FILE = ".rkp/exclude"
BUNDLE_OUTPUT_DIRS = (".rkp/knowledge/", ".rkp/knowledge.new/", ".rkp/knowledge.old/", ".rkp/cache/")
BUNDLE_FILES = frozenset({".rkp/config.json", ".rkp/overrides.json", ".rkp/.gitignore", ".rkp/.gitattributes"})
POLICY_VERSION = "exclusions-v2"
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")

# GIT_NO_LAZY_FETCH: in a partial-clone mirror a missing blob is an error, never a network call.
_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0", "PATH": os.environ.get("PATH", "")}


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    cmd = ["git", "-C", str(repo), "-c", "core.hooksPath=" + os.devnull, *args]
    return subprocess.run(cmd, env=_ENV, input=stdin, check=True, capture_output=True).stdout


def _glob(pattern: str) -> re.Pattern[str]:
    """A gitignore-style pattern relative to its base directory, as a regex over the path below it."""
    anchored = "/" in pattern.rstrip("/")
    pattern = pattern.strip("/")
    parts, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?"); i += 3
        elif pattern.startswith("**", i):
            parts.append(".*"); i += 2
        else:
            c = pattern[i]
            parts.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)); i += 1
    body = "".join(parts)
    # A directory pattern also matches everything below it; an unanchored one matches at any depth.
    return re.compile(("" if anchored else "(?:.*/)?") + body + "(?:/.*)?")


@dataclass(frozen=True)
class _Rule:
    base: str  # directory the rule is relative to ("" for the root)
    pattern: re.Pattern[str]
    source: str
    attrs: dict[str, bool] = field(default_factory=dict)  # linguist-generated / linguist-vendored -> set or unset

    def matches(self, path: str) -> bool:
        if self.base and not path.startswith(self.base + "/"):
            return False
        return bool(self.pattern.fullmatch(path[len(self.base) + 1 :] if self.base else path))


@dataclass(frozen=True)
class ExclusionPolicy:
    rules: tuple[_Rule, ...] = ()

    @classmethod
    def from_files(cls, files: dict[str, bytes]) -> ExclusionPolicy:
        """Build the policy from the commit's `.gitattributes` files and `.rkp/exclude`, shallow before deep."""
        rules: list[_Rule] = []
        for path in sorted(p for p in files if posixpath.basename(p) == ".gitattributes"):
            base = posixpath.dirname(path)
            for line in files[path].decode("utf-8", "replace").splitlines():
                tokens = line.split()
                if not tokens or tokens[0].startswith("#"):
                    continue
                attrs: dict[str, bool] = {}
                for token in tokens[1:]:
                    name, _, value = token.lstrip("-!").partition("=")
                    if name in ("linguist-generated", "linguist-vendored"):
                        attrs[name] = not token.startswith(("-", "!")) and value.lower() not in ("false", "0")
                if attrs:
                    rules.append(_Rule(base, _glob(tokens[0]), path, attrs))
        for line in files.get(POLICY_FILE, b"").decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                rules.append(_Rule("", _glob(line), POLICY_FILE))
        return cls(tuple(rules))

    def digest(self) -> str:
        return digest({"version": POLICY_VERSION, "rules": [[r.base, r.pattern.pattern, r.source, sorted(r.attrs.items())] for r in self.rules]})

    def reason(self, path: str, mode: str, obj_type: str) -> tuple[str, dict[str, Any] | None] | None:
        if obj_type == "commit":
            return "submodule", None
        if mode == "120000":
            return "symlink", None
        if _is_policy_file(path):
            return None  # the policy itself is always read, so it is pinned with the commit
        reason: tuple[str, dict[str, Any] | None] | None = None
        if any(part in EXCLUDED_DIRS for part in path.split("/")[:-1]):
            reason = ("vendored_or_generated_dir", None)
        elif posixpath.splitext(path)[1].lower() in BINARY_EXT:
            reason = ("binary", None)
        # Later and deeper rules override earlier ones, as Git and GitHub apply attributes.
        for rule in self.rules:
            if not rule.matches(path):
                continue
            if not rule.attrs:
                reason = ("repository_policy", {"rule": rule.source})
                continue
            for attr, on in rule.attrs.items():
                if on:
                    reason = ("generated" if attr == "linguist-generated" else "vendored", {"rule": rule.source})
                elif reason and reason[0] in ("vendored_or_generated_dir", "generated", "vendored"):
                    reason = None  # unset: the repository says this is its own source
        return reason


def in_bundle(path: str) -> bool:
    """A path the knowledge bundle writes or keys itself on (`repo_intel.knowledge.bundle`).

    These are never input to a compile, not even as an exclusion: otherwise committing
    the knowledge would change the source tree hash it is keyed by. Owner overrides are
    an input, but through the override set hash, not as source. Other `.rkp/` files
    (`exclude`, `business.yaml`) are read as before.
    """
    return path.startswith(BUNDLE_OUTPUT_DIRS) or path in BUNDLE_FILES


def _listing(repo: Path, commit_sha: str) -> list[tuple[str, str, str, str]]:
    # No --long: sizes come from the blobs read, so excluded blobs need never be present locally.
    out = _git(repo, "ls-tree", "-r", "-z", commit_sha)
    tree = []
    for record in filter(None, out.split(b"\0")):
        meta, path = record.split(b"\t", 1)
        mode, obj_type, oid = meta.decode().split()
        name = path.decode("utf-8", "surrogateescape")
        if not in_bundle(name):
            tree.append((name, mode, obj_type, oid))
    tree.sort()
    return tree


def _is_policy_file(path: str) -> bool:
    # A .gitattributes inside node_modules or vendor/ is not the repository's own policy.
    return (path == POLICY_FILE or posixpath.basename(path) == ".gitattributes") and not any(part in EXCLUDED_DIRS for part in path.split("/")[:-1])


def _policy_paths(tree: list[tuple[str, str, str, str]]) -> dict[str, str]:
    return {p: oid for p, mode, typ, oid in tree if typ == "blob" and mode != "120000" and _is_policy_file(p)}


def wanted_blobs(repo: str | os.PathLike[str], commit_sha: str, read: Callable[[list[str]], dict[str, bytes]] | None = None) -> tuple[list[str], list[str]]:
    """The blob ids a snapshot of `commit_sha` reads: (policy files, everything else).

    A fetcher calls this twice on a partial clone: once to learn which policy blobs to fetch,
    then with them present to learn the rest. `read` defaults to reading local objects.
    """
    repo = Path(repo)
    tree = _listing(repo, commit_sha)
    policy_oids = sorted(set(_policy_paths(tree).values()))
    present = sorted(set(policy_oids) - set(missing_objects(repo, policy_oids)))
    blobs = (read or (lambda oids: _read_blobs(repo, oids)))(present)
    files = {p: blobs[oid] for p, oid in _policy_paths(tree).items() if oid in blobs}
    policy = ExclusionPolicy.from_files(files)
    rest = {oid for p, mode, typ, oid in tree if policy.reason(p, mode, typ) is None or (mode == "120000" and typ == "blob")}
    return policy_oids, sorted(rest - set(policy_oids))


def missing_objects(repo: str | os.PathLike[str], oids: list[str]) -> list[str]:
    if not oids:
        return []
    out = _git(Path(repo), "cat-file", "--batch-check", stdin=("\n".join(oids) + "\n").encode())
    return [line.split()[0] for line in out.decode().splitlines() if line.endswith(" missing")]


def _read_blobs(repo: Path, oids: list[str]) -> dict[str, bytes]:
    if not oids:
        return {}
    out = _git(repo, "cat-file", "--batch", stdin=("\n".join(oids) + "\n").encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in oids:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].split()
        if header[-1:] == [b"missing"]:
            raise RuntimeError(f"blob {oid} is not in the mirror; fetch the commit before snapshotting it")
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

    tree = _listing(repo, commit_sha)
    policy_oids, rest = wanted_blobs(repo, commit_sha)
    blobs = _read_blobs(repo, sorted(set(policy_oids + rest)))
    return {"commit_sha": commit_sha, **_path_manifest(store, tree, blobs)}


def _git_blob_oid(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _git_blob_oid_of_file(path: Path, size: int) -> str:
    digest_ = hashlib.sha1(b"blob %d\0" % size)
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest_.update(chunk)
    return digest_.hexdigest()


def snapshot_worktree(store: ObjectStore, repo_path: str | os.PathLike[str], base_commit: str) -> dict[str, Any]:
    """The path manifest of the working tree as it is on disk, uncommitted changes included.

    The same as `snapshot_commit` would give for a commit of every tracked and untracked,
    not ignored file, so the source tree hash matches once those changes are committed
    (Git's clean filters, such as line-ending conversion, are not applied). Nothing is
    written to the repository: object IDs are computed, not stored. Excluded files are
    hashed as a stream and never held in memory. `base_commit` is the commit it is on top of.
    """
    if not _SHA.match(base_commit):
        raise ValueError("base_commit must be a full lowercase hex SHA")
    repo = Path(repo_path)
    staged = {}
    for record in filter(None, _git(repo, "ls-files", "-z", "--stage").split(b"\0")):
        meta, path = record.split(b"\t", 1)
        mode, oid, _stage = meta.decode().split()
        staged[path.decode("utf-8", "surrogateescape")] = (mode, oid)
    untracked = [p.decode("utf-8", "surrogateescape") for p in filter(None, _git(repo, "ls-files", "-z", "--others", "--exclude-standard").split(b"\0"))]

    listing: list[tuple[str, str, str, Path | None, int]] = []  # path, mode, type, file on disk, size
    gitlinks: dict[str, str] = {}
    for name in sorted(set(staged) | set(untracked)):
        if name.endswith("/") or in_bundle(name):
            continue
        if staged.get(name, ("",))[0] == "160000":
            gitlinks[name] = staged[name][1]
            listing.append((name, "160000", "commit", None, 0))
            continue
        file = repo / name
        try:
            info = file.lstat()
        except FileNotFoundError:
            continue  # deleted in the working tree
        if stat.S_ISLNK(info.st_mode):
            listing.append((name, "120000", "blob", file, 0))
        elif stat.S_ISREG(info.st_mode):
            listing.append((name, "100755" if info.st_mode & stat.S_IXUSR else "100644", "blob", file, info.st_size))

    return {"commit_sha": base_commit, "working_tree": True, **_disk_manifest(store, listing, gitlinks)}


def snapshot_folder(store: ObjectStore, folder: Path) -> dict[str, Any]:
    """Pin a folder's files without making a Git repository or following symlinks."""
    listing = []
    for parent, dirs, files in os.walk(folder, followlinks=False, onerror=_raise_walk_error):
        root = Path(parent)
        # Prune Git metadata and RKP's own output before descending, including nested Git metadata.
        dirs[:] = [name for name in dirs if name != ".git" and not in_bundle((root / name).relative_to(folder).as_posix() + "/")]
        for name in files + dirs:
            file = root / name
            relative = file.relative_to(folder).as_posix()
            if name == ".git" or in_bundle(relative):
                continue
            try:
                info = file.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                listing.append((relative, "120000", "blob", file, 0))
            elif stat.S_ISREG(info.st_mode):
                mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
                listing.append((relative, mode, "blob", file, info.st_size))
    manifest = _disk_manifest(store, listing, {})
    # The bundle protocol uses commit_sha as the revision ID. Folders use their full
    # content digest so the same files produce the same revision on every scan.
    return {"commit_sha": manifest["source_tree_hash"].split(":", 1)[1], **manifest}


def _raise_walk_error(error: OSError) -> None:
    raise error


def _disk_manifest(
    store: ObjectStore, listing: list[tuple[str, str, str, Path | None, int]], gitlinks: dict[str, str],
) -> dict[str, Any]:
    def read(file: Path, mode: str) -> bytes:
        return os.fsencode(os.readlink(file)) if mode == "120000" else file.read_bytes()

    policy_files = {name: read(file, mode) for name, mode, typ, file, _ in listing if typ == "blob" and mode != "120000" and _is_policy_file(name) and file}
    policy = ExclusionPolicy.from_files(policy_files)
    tree: list[tuple[str, str, str, str]] = []
    blobs: dict[str, bytes] = {}
    for name, mode, typ, file, size in listing:
        if typ == "commit":
            tree.append((name, mode, typ, gitlinks[name]))
        elif policy.reason(name, mode, typ) is None or mode == "120000":
            data = read(file, mode)  # type: ignore[arg-type]
            oid = _git_blob_oid(data)
            blobs[oid] = data
            tree.append((name, mode, typ, oid))
        else:
            tree.append((name, mode, typ, _git_blob_oid_of_file(file, size)))  # type: ignore[arg-type]
    tree.sort()
    return _path_manifest(store, tree, blobs)


def _path_manifest(store: ObjectStore, tree: list[tuple[str, str, str, str]], blobs: dict[str, bytes]) -> dict[str, Any]:
    """Entries, exclusions and the source tree hash for a listing whose readable blobs are in `blobs`."""
    policy = ExclusionPolicy.from_files({p: blobs[oid] for p, oid in _policy_paths(tree).items()})

    entries: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for path, mode, obj_type, oid in tree:
        excluded = policy.reason(path, mode, obj_type)
        if excluded:
            reason, detail = excluded
            item: dict[str, Any] = {"path": path, "reason": reason, "git_oid": oid}
            if reason == "symlink":
                target = blobs[oid].decode("utf-8", "replace")
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
                detail = {"escapes_root": target.startswith("/") or resolved.startswith("..")}
            if detail:
                item["detail"] = detail
            exclusions.append(item)
            continue
        data = blobs[oid]
        blob_sha = sha256_hex(data)
        store.put_bytes(data)
        entry = {"path": path, "blob_sha256": blob_sha, "git_oid": oid, "size": len(data), "mode": mode, "parse": len(data) <= MAX_PARSE_BYTES}
        findings = secrets.scan(data)
        if findings:  # location and detector only; the value is never recorded
            entry["secret_findings"] = findings
        entries.append(entry)

    body: dict[str, Any] = {"entries": entries, "exclusions": exclusions}
    if policy.rules:  # recorded only when the repository has a policy, so existing hashes stay stable
        body["exclusion_policy"] = policy.digest()
    return {**body, "source_tree_hash": digest(body)}
