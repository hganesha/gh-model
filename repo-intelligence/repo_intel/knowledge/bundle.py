"""The knowledge bundle: a compiled snapshot as plain files under `.rkp/` in the repository.

No database and no server: the bundle is read straight from the files, and is
reused whenever the repository is opened again. Layout (format `rkp-bundle/2`):

    .rkp/config.json                 repository ID, bundle format and what to commit; written once, then the owner's
    .rkp/overrides.json              owner overrides, optional (the OverrideDocument shape)
    .rkp/exclude                     the repository's exclusion globs (read by the snapshot)
    .rkp/.gitignore, .gitattributes  ignore cache/; mark knowledge/ as generated
    .rkp/knowledge/manifest.json     what was compiled, by which toolchain, and the unit files
    .rkp/knowledge/units/<slug>.jsonl  one row per line: {"table": ..., **row}, in the engine's order,
                                        fields equal to ROW_DEFAULTS left out; "behavior" rows
                                        (units of the behavioural IR) only in a full bundle
    .rkp/knowledge/files.jsonl       every path read or excluded, with its blob hash; never content
    .rkp/knowledge/coverage.jsonl, conflicts.jsonl, unknowns.jsonl, decisions.jsonl
    .rkp/knowledge/exports.json      the repository's exports for linking it to others: contract
                                        operations it declares and HTTP calls no endpoint of its own
                                        serves; in every tier, named in manifest.json with its digest
    .rkp/cache/                     object store: blobs, step cache, decision ledger; not committed

What is committed depends on `"commit"` in config.json. `"architecture"` (the default)
writes the architecture tier: every entity except function-level ones (DETAIL_KINDS), the
assertions and unknowns that do not touch them, and the evidence those rows cite. Behaviour
rows (DETAIL_TABLES) are function-level detail too and are left out whole. The
full snapshot stays in `cache/`, and a fresh clone gets it back with a local compile; until
then the bundle is read as it is. `"full"` writes everything.

Every file is written deterministically (sorted keys, the engine's row order, no timestamps), so
compiling the same source with the same toolchain rewrites identical bytes and a
committed bundle shows a readable diff. Evidence is stored as spans with a blob
hash; excerpt text stays in the cache and in the source itself.

The snapshot leaves the files written here out of its input (`ingest.snapshot.in_bundle`),
so committing the bundle does not change the source tree hash it is keyed by. Other
`.rkp/` files (`exclude`, `business.yaml`) are repository input as before.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from repo_intel.compile.contracts import Manifest, ShardRef
from repo_intel.compile.pipeline import load_shards, stage_compile, stage_snapshot, toolchain_hash
from repo_intel.decisions.gateway import Gateway
from repo_intel.decisions.overrides import EMPTY, OverrideDocument, Overrides
from repo_intel.ingest.snapshot import snapshot_commit, snapshot_worktree
from repo_intel.storage.hashing import digest, sha256_hex
from repo_intel.storage.store import ObjectStore

FORMAT = "rkp-bundle/2"
READABLE_FORMATS = frozenset({"rkp-bundle/1", FORMAT})

# Format 2 leaves a field out of a unit-file row when it holds this value; readers put it
# back (manifest.json repeats the table as `row_defaults`). Fixed by the format, not
# computed per bundle, so a row's text never changes because other rows did. A test keeps
# them consistent with the contract defaults in repo_intel.compile.contracts.
ROW_DEFAULTS: dict[str, dict[str, Any]] = {
    "entity": {"attributes": {}, "evidence_ids": []},
    "assertion": {
        "assertion_kind": "derived_relation", "epistemic_status": "supported", "resolution": "exact",
        "qualifiers": {}, "counterevidence_ids": [], "confidence": None, "decision_ref": None,
        "verification_ref": None, "sensitivity": "internal",
    },
    "evidence": {"ast_node_kind": None, "trust_label": "untrusted_data", "sensitivity": "internal", "secret_masked": False},
    "unknown": {"status": "unknown"},
}
# Entity kinds left out of the architecture tier, with every assertion and unknown that
# touches one and the evidence only those rows cite. Symbol (functions, classes, methods)
# and its calls, test links and definitions are most of a bundle's bytes.
DETAIL_KINDS = ("Symbol",)
# Shard tables that are detail whole: the behaviour of functions, pages and steps
# (repo_intel.compile.behavior), keyed by owner. A shard holds the table only when it has
# rows, so a snapshot without behaviour hashes as it did before the table existed. The
# transform shapes of those units (repo_intel.compile.transforms) are detail the same way.
DETAIL_TABLES = ("behaviors", "transforms")
TIERS = ("architecture", "full")
DEFAULT_TIER = "architecture"
BUNDLE = ".rkp"
TENANT = "local"
_TABLES = (("entities", "entity"), ("assertions", "assertion"), ("evidence", "evidence"), ("unknowns", "unknown"))
# Transform rows are written before behaviour rows, so behaviour rows stay the last in a unit file.
_OPTIONAL_TABLES = (("transforms", "transform"), ("behaviors", "behavior"))  # present in a shard only when it has rows
_ALL_TABLES = _TABLES + _OPTIONAL_TABLES
ROW_TABLES = tuple(table for table, _ in _TABLES)  # in every shard
OPTIONAL_TABLES = tuple(table for table, _ in _OPTIONAL_TABLES)  # in a shard only when it has rows
ROW_NAMES = dict(_ALL_TABLES)  # shard table -> the `table` value its rows carry in a unit file
_GITIGNORE = "# Written by repo-intel: the cache is local to each machine.\ncache/\nknowledge.new/\nknowledge.old/\n"
_GITATTRIBUTES = "# Written by repo-intel: compiled knowledge, regenerated by `rkp compile`.\nknowledge/** linguist-generated=true\n"
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class BundleError(RuntimeError):
    pass


def engine_version() -> str:
    try:
        return version("repo-intel")
    except PackageNotFoundError:
        return "0+unknown"


def _git(repo: Path, *args: str) -> str:
    # --no-optional-locks: `status` must not refresh the index under the user's own git.
    try:
        return subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args], check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise BundleError(f"git {' '.join(args)} failed in {repo}") from error


def repository_root(path: str | os.PathLike[str]) -> Path:
    return Path(_git(Path(path), "rev-parse", "--show-toplevel"))


def resolve_commit(repo: Path, ref: str = "HEAD") -> str:
    if ref.startswith("-"):
        raise BundleError("a ref cannot start with '-'")
    sha = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    if not _SHA.match(sha):
        raise BundleError(f"unexpected commit id {sha!r}")
    return sha


def _remote_id(url: str) -> str | None:
    """`github.com/owner/name` from an HTTPS or SSH remote URL; None when it does not parse."""
    url = url.strip()
    match = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^/:]+)[:/](.+?)(?:\.git)?/?$", url)
    if not match:
        return None
    host, path = match.groups()
    path = path.lstrip("/")
    return f"{host.lower()}/{path}" if host and path and ".." not in path else None


def default_repository_id(repo: Path) -> str:
    """The origin remote as host/path, else `local/<directory name>`. Never an absolute path,
    so every clone of a repository derives the same entity IDs."""
    try:
        remote = _remote_id(_git(repo, "remote", "get-url", "origin"))
    except BundleError:
        remote = None
    return remote or f"local/{re.sub(r'[^A-Za-z0-9._-]+', '-', repo.name) or 'repository'}"


def read_config(repo: Path) -> dict[str, Any]:
    path = repo / BUNDLE / "config.json"
    if not path.is_file():
        return {}
    try:
        config = json.loads(path.read_text())
    except ValueError as error:
        raise BundleError(f"{path} is not valid JSON") from error
    if not isinstance(config, dict):
        raise BundleError(f"{path} must hold a JSON object")
    return config


def commit_tier(config: dict[str, Any]) -> str:
    """What `rkp compile` commits: `"commit"` in .rkp/config.json, `architecture` by default."""
    tier = config.get("commit", DEFAULT_TIER)
    if tier not in TIERS:
        raise BundleError(f'.rkp/config.json: "commit" must be one of {", ".join(TIERS)}, not {tier!r}')
    return tier


def ensure_layout(repo: Path) -> dict[str, Any]:
    """Create `.rkp/` with its config, .gitignore and .gitattributes. Returns the config."""
    root = repo / BUNDLE
    root.mkdir(exist_ok=True)
    config = read_config(repo)
    if not config.get("repository_id"):
        config = {"bundle_format": FORMAT, "repository_id": default_repository_id(repo), "commit": DEFAULT_TIER, **config}
        _write_json(root / "config.json", config)
    for name, text in ((".gitignore", _GITIGNORE), (".gitattributes", _GITATTRIBUTES)):
        path = root / name
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
    return config


def load_overrides(repo: Path) -> Overrides:
    path = repo / BUNDLE / "overrides.json"
    if not path.is_file():
        return EMPTY
    try:
        return Overrides.of(OverrideDocument.model_validate_json(path.read_text()))
    except ValueError as error:
        raise BundleError(f"{path} is not a valid override document: {error}") from error


def store_for(repo: Path) -> ObjectStore:
    return ObjectStore(repo / BUNDLE / "cache", TENANT)


# ------------------------------------------------------------------ writing


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n")


def _omit_defaults(table: str, row: dict[str, Any]) -> dict[str, Any]:
    # Compared as canonical JSON: in Python 0 == False and 1 == 1.0, which JSON tells apart.
    defaults = ROW_DEFAULTS.get(table, {})
    return {k: v for k, v in row.items() if k not in defaults or _dumps(v) != _dumps(defaults[k])}


def _with_defaults(row: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    return {**json.loads(_dumps(defaults)), **row}


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(_dumps(row) + "\n" for row in rows))


def _slugs(units: list[str]) -> dict[str, str]:
    named: dict[str, str] = {}
    taken: set[str] = set()
    for unit in sorted(units):
        slug = {".": "root", "@global": "global"}.get(unit) or re.sub(r"[^A-Za-z0-9._-]+", "-", unit).strip("-.") or "unit"
        if slug in taken or slug in ("root", "global") and unit not in (".", "@global"):
            slug = f"{slug}-{sha256_hex(unit.encode())[:8]}"
        taken.add(slug)
        named[unit] = slug
    return named


def _architecture_shards(shards: dict[str, dict[str, Any]], conflicts: list[dict[str, Any]], keep_evidence: frozenset[str] = frozenset()) -> dict[str, dict[str, Any]]:
    """The shards without function-level detail; rows keep the engine's order. Evidence in
    `keep_evidence` (cited by the exports file) is kept like evidence the rows cite."""
    detail = {e["id"] for shard in shards.values() for e in shard["entities"] if e["kind"] in DETAIL_KINDS}
    kept: dict[str, dict[str, Any]] = {}
    cited = {ev for c in conflicts for ev in c.get("evidence_ids", [])} | set(keep_evidence)
    for unit, shard in shards.items():
        entities = [e for e in shard["entities"] if e["id"] not in detail]
        assertions = [a for a in shard["assertions"] if a["subject"] not in detail and a["object"] not in detail]
        unknowns = [u for u in shard["unknowns"] if u["subject"] not in detail]
        for row in (*entities, *assertions):
            cited.update(row.get("evidence_ids", []), row.get("counterevidence_ids", []))
        rest = {k: v for k, v in shard.items() if k not in DETAIL_TABLES}
        kept[unit] = {**rest, "entities": entities, "assertions": assertions, "unknowns": unknowns}
    for shard in kept.values():
        shard["evidence"] = [ev for ev in shard["evidence"] if ev["id"] in cited]
    return kept


def write_bundle(repo: Path, store: ObjectStore, manifest: Manifest, *, working_tree: bool = False, tier: str = "full") -> dict[str, Any]:
    """Write `.rkp/knowledge/` for a published manifest, replacing what is there. Returns manifest.json.

    Rows keep the engine's order, so `hydrate` can rebuild every stored object from the
    files and check it against the digest the manifest names. In the architecture tier each
    unit's digest is that of its reduced shard, which `hydrate` checks the same way.
    """
    if tier not in TIERS:
        raise BundleError(f"unknown tier {tier!r}")
    root = repo / BUNDLE
    staging, previous, target = root / "knowledge.new", root / "knowledge.old", root / "knowledge"
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "units").mkdir(parents=True)

    shards = load_shards(store, manifest)
    full = shards
    conflicts = store.get_json(manifest.conflicts_ref)
    exports = compiled_exports(store, manifest)
    if tier == "architecture":
        shards = _architecture_shards(shards, conflicts, export_evidence(exports))
    slugs = _slugs(list(shards))
    units, totals = [], {table: 0 for table, _ in _ALL_TABLES}
    for unit, shard in sorted(shards.items()):
        extra = set(shard) - {"unit", *(table for table, _ in _ALL_TABLES)}
        if extra or shard.get("unit") != unit:
            raise BundleError(f"shard {unit!r} has a shape this bundle format does not hold: {sorted(extra)}")
        rows, counts = [], {}
        for table, name in _ALL_TABLES:
            items = shard.get(table, [])
            counts[table] = len(items)
            totals[table] += len(items)
            rows.extend({"table": name, **_omit_defaults(name, row)} for row in items)
        file = f"units/{slugs[unit]}.jsonl"
        _write_jsonl(staging / file, rows)
        units.append({"unit": unit, "file": file, **counts, **({"digest": store.put_json(shard)} if tier == "architecture" else {})})

    coverage = store.get_json(manifest.coverage_ref)
    unknowns = store.get_json(manifest.unknowns_ref)
    decisions = [store.get_json(e["digest"]) | {"decision_id": e["decision_id"]} for e in store.get_json(manifest.ledger_entries_ref)]
    path_manifest = store.get_json(store.index_get("path_manifests", manifest.source_tree_hash) or "")
    files = [{"path": e["path"], "blob_sha256": e["blob_sha256"], "size": e["size"], "parsed": e["parse"], **({"secret_findings": len(e["secret_findings"])} if e.get("secret_findings") else {})} for e in path_manifest["entries"]]
    files += [{"path": x["path"], "excluded": x["reason"]} for x in path_manifest["exclusions"]]
    for name, rows in (("coverage", coverage), ("conflicts", conflicts), ("unknowns", unknowns), ("decisions", decisions), ("files", sorted(files, key=lambda f: f["path"]))):
        _write_jsonl(staging / f"{name}.jsonl", rows)
    exports_entry = _write_exports(staging, store, exports)

    document = {
        "bundle_format": FORMAT,
        "engine": {"name": "repo-intel", "version": engine_version()},
        "repository_id": manifest.repository_id, "commit_sha": manifest.commit_sha, "working_tree": working_tree,
        "source_tree_hash": manifest.source_tree_hash, "toolchain_hash": manifest.toolchain_hash,
        "override_set_hash": manifest.override_set_hash, "rkp_hash": manifest.rkp_hash, "status": manifest.status,
        "row_defaults": ROW_DEFAULTS,
        "tier": tier,
        **({"detail_kinds": list(DETAIL_KINDS), "detail_tables": list(DETAIL_TABLES), "detail_omitted": {table: sum(len(s.get(table, [])) for s in full.values()) - totals[table] for table, _ in _ALL_TABLES}} if tier == "architecture" else {}),
        "units": units,
        "counts": {**totals, "coverage": len(coverage), "conflicts": len(conflicts), "repository_unknowns": len(unknowns), "decisions": len(decisions), "files": sum(1 for f in files if "blob_sha256" in f), "excluded_files": sum(1 for f in files if "excluded" in f)},
        **({"exports": exports_entry} if exports_entry else {}),
        "snapshot": manifest.row(),
    }
    _write_json(staging / "manifest.json", document)

    shutil.rmtree(previous, ignore_errors=True)
    if target.exists():
        target.rename(previous)
    staging.rename(target)
    shutil.rmtree(previous, ignore_errors=True)
    return document


# ------------------------------------------------------------------ repository exports
#
# `workspace_exports`, which the compiler writes beside the snapshot: the contract operations
# the repository declares and the HTTP calls no endpoint of its own serves, each with the
# entity it belongs to and the evidence it cites. A solution or a workspace links one
# repository to another with them. They are small and are needed without a local compile,
# so they are committed in every tier as one JSON file that manifest.json names with its
# digest. They are not part of the snapshot's content hash, so the digest in manifest.json
# is what they are checked against. Bundles written before the file existed do not name it
# and still read; their exports are simply not known.

EXPORTS_FILE = "exports.json"
EXPORT_LISTS = ("operations", "unresolved_calls")


def compiled_exports(store: ObjectStore, manifest: Manifest) -> dict[str, list[dict[str, Any]]] | None:
    """The exports the compile of `manifest` stored, or None when the cache does not hold them."""
    ref = store.index_get("workspace_exports", manifest.rkp_hash)
    return store.get_json(ref) if ref and store.has(ref) else None


def export_evidence(exports: dict[str, Any] | None) -> frozenset[str]:
    """Every evidence ID the exports cite."""
    if not exports:
        return frozenset()
    return frozenset(ev for name in EXPORT_LISTS for row in exports.get(name, []) for ev in row.get("evidence_ids", []))


def exports_problem(value: Any) -> str | None:
    """Why `value` is not an exports document, or None when it is one."""
    if not isinstance(value, dict) or set(value) != set(EXPORT_LISTS):
        return f"does not hold exactly {' and '.join(EXPORT_LISTS)}"
    for name in EXPORT_LISTS:
        if not isinstance(value[name], list) or not all(isinstance(row, dict) for row in value[name]):
            return f"{name} is not a list of objects"
    return None


def _write_exports(staging: Path, store: ObjectStore, exports: dict[str, Any] | None) -> dict[str, Any] | None:
    """Write exports.json; the manifest.json entry naming it, or None when there are none to write."""
    if exports is None:
        return None
    _write_json(staging / EXPORTS_FILE, exports)
    return {"file": EXPORTS_FILE, "digest": store.put_json(exports), **{name: len(exports[name]) for name in EXPORT_LISTS}}


def _hydrate_exports(knowledge: Path, document: dict[str, Any], problems: list[str]) -> dict[str, Any] | None:
    """The committed exports, checked against manifest.json; None when the bundle has none."""
    entry = document.get("exports")
    if entry is None:
        return None
    if not isinstance(entry, dict) or entry.get("file") != EXPORTS_FILE:
        problems.append("exports")
        return None
    try:
        value = json.loads((knowledge / EXPORTS_FILE).read_text())
    except (FileNotFoundError, ValueError):
        problems.append("exports")
        return None
    if exports_problem(value) is not None or digest(value) != entry.get("digest"):
        problems.append("exports")
        return None
    return value


def read_exports(repo: Path) -> dict[str, Any] | None:
    """The exports committed in `repo`'s bundle, checked against its manifest; None when the
    bundle predates them. Raises BundleError when the file does not match its digest."""
    document = read_manifest(repo)
    if document is None:
        return None
    problems: list[str] = []
    value = _hydrate_exports(repo / BUNDLE / "knowledge", document, problems)
    if problems:
        raise BundleError(f"{EXPORTS_FILE} does not match its manifest; compile again")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        return []
    except ValueError as error:
        raise BundleError(f"{path} is not valid JSON lines") from error


def hydrate(repo: Path, store: ObjectStore | None = None) -> tuple[ObjectStore, Manifest]:
    """Load the committed bundle into the local cache, so a fresh clone can be read like a compile.

    Every object is rebuilt from the files and must hash to the digest the snapshot manifest
    names; a bundle edited by hand or damaged in a merge is refused, never half-loaded.
    Source blobs are not in the bundle: excerpts are read from the working tree instead.

    An architecture-tier bundle cannot rebuild the full shards. Its units are checked
    against their own digests in manifest.json, and the manifest returned names those
    reduced shards; it is not indexed as the snapshot, which only a compile produces.
    """
    document = read_manifest(repo)
    if document is None:
        raise BundleError("no bundle has been written; run `rkp compile` first")
    manifest = Manifest.model_validate(document["snapshot"])
    store = store or store_for(repo)
    knowledge = repo / BUNDLE / "knowledge"
    problems = []

    def put(value: Any, expected: str, what: str) -> None:
        # Always check the digest: a warm cache must not hide a hand-edited unit file.
        if digest(value) != expected:
            problems.append(what)
        elif not store.has(expected):
            store.put_json(value)

    defaults = document.get("row_defaults", {})  # format 1 wrote every field
    architecture = document.get("tier", "full") == "architecture"
    shard_refs = []
    by_unit = {u["unit"]: u for u in document["units"]}
    for ref in manifest.shards:
        unit = by_unit.get(ref.unit)
        if unit is None:
            problems.append(f"unit {ref.unit}")
            continue
        file = (knowledge / unit["file"]).resolve()
        if not file.is_relative_to(knowledge.resolve()):
            raise BundleError(f"unit file {unit['file']} is outside the bundle")
        shard: dict[str, Any] = {"unit": ref.unit, **{table: [] for table, _ in _TABLES}}
        names = {name: table for table, name in _ALL_TABLES}
        for row in _read_jsonl(file):
            name = row.pop("table", None)
            if name not in names:
                raise BundleError(f"{unit['file']}: rows of table {name!r} are not part of format {document['bundle_format']}")
            shard.setdefault(names[name], []).append(_with_defaults(row, defaults.get(name, {})))
        expected = unit.get("digest", "") if architecture else ref.digest
        put(shard, expected, f"unit {ref.unit}")
        shard_refs.append(ShardRef(unit=ref.unit, digest=expected))
    for name, ref in (("coverage", manifest.coverage_ref), ("conflicts", manifest.conflicts_ref), ("unknowns", manifest.unknowns_ref)):
        put(_read_jsonl(knowledge / f"{name}.jsonl"), ref, name)
    entries = []
    for decision in _read_jsonl(knowledge / "decisions.jsonl"):
        entries.append({"decision_id": decision["decision_id"], "digest": store.put_json(decision)})
    put(entries, manifest.ledger_entries_ref, "decisions")
    exports = _hydrate_exports(knowledge, document, problems)
    if problems:
        raise BundleError("the bundle does not match its manifest (" + ", ".join(problems) + "); compile again")
    if exports is not None:  # complete in every tier, so indexed as the compile indexes it
        store.index_put("workspace_exports", manifest.rkp_hash, store.put_json(exports))
    if architecture:
        return store, manifest.model_copy(update={"shards": shard_refs})
    store.index_put("rkp", manifest.rkp_hash, store.put_json(manifest.row()))
    return store, manifest


def has_snapshot(store: ObjectStore, manifest: Manifest) -> bool:
    """Whether the cache holds every object of the compiled snapshot, detail included."""
    refs = [ref.digest for ref in manifest.shards] + [manifest.coverage_ref, manifest.conflicts_ref, manifest.unknowns_ref, manifest.ledger_entries_ref]
    return all(store.has(d) for d in refs)


# ------------------------------------------------------------------ commands


def _snapshot(store: ObjectStore, repo: Path, sha: str, working_tree: bool) -> str:
    """Pin the source (the commit, or the working tree on top of it); the path manifest digest."""
    if not working_tree:
        return stage_snapshot(store, repo, sha)
    pm = snapshot_worktree(store, repo, sha)
    digest_ = store.put_json(pm)
    store.index_put("path_manifests", pm["source_tree_hash"], digest_)
    return digest_


def compile_bundle(path: str | os.PathLike[str], ref: str = "HEAD", *, working_tree: bool = False, gateway: Gateway | None = None, progress: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Compile `ref` (or, with `working_tree`, the files on disk on top of it) and write the bundle."""
    repo = repository_root(path)
    config = ensure_layout(repo)
    sha = resolve_commit(repo, ref)
    store = store_for(repo)
    overrides = load_overrides(repo)
    result = stage_compile(store, config["repository_id"], _snapshot(store, repo, sha, working_tree), gateway=gateway, overrides=overrides, progress=progress)
    document = write_bundle(repo, store, result.manifest, working_tree=working_tree, tier=commit_tier(config))
    if progress:
        progress("written", {"path": str(repo / BUNDLE / "knowledge"), "steps_executed": result.stats.total_executed, "steps_reused": sum(result.stats.hits.values())})
    return document


def read_manifest(repo: Path) -> dict[str, Any] | None:
    path = repo / BUNDLE / "knowledge" / "manifest.json"
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except ValueError as error:
        raise BundleError(f"{path} is not valid JSON") from error
    if document.get("bundle_format") not in READABLE_FORMATS:
        raise BundleError(f"{path} has format {document.get('bundle_format')!r}; this engine reads {', '.join(sorted(READABLE_FORMATS))}")
    return document


def bundle_status(path: str | os.PathLike[str], ref: str = "HEAD", *, working_tree: bool = False, gateway: Gateway | None = None) -> dict[str, Any]:
    """Whether the bundle describes `ref` (or the working tree): `missing`, `fresh` or `stale`.

    Compares the source tree hash (not the commit, so a bundle committed alongside the
    source it describes stays fresh), the toolchain, the owner overrides, the bundle format
    and the `"commit"` setting. `detail` says whether function-level detail can be read here:
    `complete` for a full bundle or when the cache holds the compiled snapshot, else
    `architecture` (a fresh clone of an architecture-tier bundle, until a local compile).
    """
    repo = repository_root(path)
    sha = resolve_commit(repo, ref)
    dirty = bool(_git(repo, "status", "--porcelain", "--untracked-files=normal", "--", ".", f":(exclude){BUNDLE}"))
    document = read_manifest(repo)
    base = {"repository": str(repo), "ref": ref, "commit_sha": sha, "working_tree": working_tree, "uncommitted_changes": dirty}
    if document is None:
        return {**base, "state": "missing", "reasons": ["no bundle has been written"]}
    store = store_for(repo)
    tree = (snapshot_worktree(store, repo, sha) if working_tree else snapshot_commit(store, repo, sha))["source_tree_hash"]
    reasons = []
    if tree != document["source_tree_hash"]:
        reasons.append("source changed")
    if toolchain_hash(gateway) != document["toolchain_hash"]:
        reasons.append("toolchain changed")
    if load_overrides(repo).set_hash != document["override_set_hash"]:
        reasons.append("owner overrides changed")
    if document["bundle_format"] != FORMAT:
        reasons.append("bundle format changed")  # still readable; compiling again rewrites it
    tier = document.get("tier", "full")
    if commit_tier(read_config(repo)) != tier:
        reasons.append("commit setting changed")
    detail = "complete" if tier == "full" or has_snapshot(store, Manifest.model_validate(document["snapshot"])) else "architecture"
    return {
        **base, "state": "stale" if reasons else "fresh", "reasons": reasons, "bundle_commit_sha": document["commit_sha"],
        "bundle_working_tree": document.get("working_tree", False), "tier": tier, "detail": detail, "counts": document["counts"],
    }
