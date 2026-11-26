"""Reading a committed bundle as it is, re-checking it, and comparing two (`rkp verify`, `rkp diff`).

A bundle is read from a `knowledge/` directory or from a commit through `git cat-file`, without
compiling and without the local cache. `bundle.hydrate` checks every digest too, but it stops at
the first table it cannot read, reports only which objects failed, needs a working tree, and
writes into the cache; `verify` instead lists every problem with the file and row it is in, and
works on any directory or commit. Problems are collected, not raised, so one run lists all of them.

Each unit's shard is rebuilt exactly as `hydrate` rebuilds it: the base tables
(`bundle.ROW_TABLES`) always, an optional table (`bundle.OPTIONAL_TABLES`, the behaviour rows)
only when it has rows. Any other difference would change the shard's digest.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from repo_intel.knowledge import bundle

_UNIT_FILE = re.compile(r"^units/[A-Za-z0-9._-]+\.jsonl$")
# The `table` field each shard table's rows carry in a unit file. A test keeps this in step
# with the tables bundle.write_bundle writes.
ROW_NAMES = bundle.ROW_NAMES
SIDE_TABLES = ("coverage", "conflicts", "unknowns", "decisions", "files")
DECISION_ATTRIBUTES = ("decision_ref", "topology_decision_ref", "primary_language_decision_ref")


def _canonical(value: Any) -> str:
    # Compared as JSON text: in Python 0 == False and 1 == 1.0, which JSON tells apart.
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _tables() -> tuple[str, ...]:
    return (*bundle.ROW_TABLES, *bundle.OPTIONAL_TABLES)


# ------------------------------------------------------------------ sources


class DirectorySource:
    """A bundle's `knowledge/` directory on disk."""

    def __init__(self, knowledge: Path, label: str) -> None:
        self.knowledge, self.label = knowledge, label
        self.escaped: list[str] = []

    def read(self, names: list[str]) -> dict[str, bytes | None]:
        root = self.knowledge.resolve()
        out: dict[str, bytes | None] = {}
        for name in names:
            path = self.knowledge / name
            if path.exists() and not path.resolve().is_relative_to(root):
                self.escaped.append(name)  # a symbolic link out of the bundle is never followed
                out[name] = None
                continue
            try:
                out[name] = path.read_bytes()
            except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
                out[name] = None
        return out

    def unit_files(self) -> list[str]:
        units = self.knowledge / "units"
        return sorted(f"units/{p.name}" for p in units.iterdir() if p.is_file()) if units.is_dir() else []


class GitSource:
    """The bundle committed at one commit, read with `git cat-file` without checking anything out."""

    def __init__(self, repo: Path, ref: str) -> None:
        self.repo, self.ref = repo, ref
        self.sha = bundle.resolve_commit(repo, ref)
        self.label = f"{ref} ({self.sha[:12]})"
        self.escaped: list[str] = []

    def _git(self, args: list[str], data: bytes | None = None) -> bytes:
        try:
            return subprocess.run(["git", "--no-optional-locks", "-C", str(self.repo), *args], input=data, check=True, capture_output=True, timeout=120).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise bundle.BundleError(f"git {args[0]} failed in {self.repo}") from error

    def read(self, names: list[str]) -> dict[str, bytes | None]:
        if not names:
            return {}
        request = "".join(f"{self.sha}:{bundle.BUNDLE}/knowledge/{name}\n" for name in names).encode()
        output = self._git(["cat-file", "--batch"], request)
        out: dict[str, bytes | None] = {}
        at = 0
        for name in names:
            end = output.index(b"\n", at)
            header = output[at:end].decode("utf-8", "replace").split()
            at = end + 1
            if len(header) != 3 or not header[2].isdigit():  # "<name> missing" and the like
                out[name] = None
                continue
            size = int(header[2])
            out[name] = output[at:at + size] if header[1] == "blob" else None
            at += size + 1
        return out

    def unit_files(self) -> list[str]:
        listing = self._git(["ls-tree", "--name-only", self.sha, "--", f"{bundle.BUNDLE}/knowledge/units/"]).decode()
        prefix = f"{bundle.BUNDLE}/knowledge/"
        return sorted(line[len(prefix):] for line in listing.splitlines() if line.startswith(prefix))


Source = DirectorySource | GitSource


def bundle_directory(path: str | Path) -> Path | None:
    """The `knowledge/` directory for a path naming a repository, its `.rkp/` or `knowledge/` itself."""
    base = Path(path)
    for candidate in (base / bundle.BUNDLE / "knowledge", base / "knowledge", base):
        if (candidate / "manifest.json").is_file():
            return candidate
    return None


def directory_source(path: str | Path) -> DirectorySource:
    knowledge = bundle_directory(path)
    if knowledge is None:
        knowledge = bundle.repository_root(path) / bundle.BUNDLE / "knowledge"
    return DirectorySource(knowledge, str(knowledge.parent.parent if knowledge.parent.name == bundle.BUNDLE else knowledge))


def named_source(name: str, repo: str | Path) -> Source:
    """A directory when `name` is one (it takes precedence over a ref of the same name), else a commit of `repo`."""
    if Path(name).is_dir():
        return directory_source(name)
    return GitSource(bundle.repository_root(repo), name)


# ------------------------------------------------------------------ loading


def _parse_lines(data: bytes, file: str, problems: list[str]) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            problems.append(f"{file}: line {number} is not valid JSON")
            continue
        if not isinstance(row, dict):
            problems.append(f"{file}: line {number} is not a JSON object")
            continue
        rows.append(row)
    return rows


def load_bundle(source: Source) -> dict[str, Any]:
    """Read a bundle's manifest and rows and rebuild each unit's shard as `bundle.hydrate` does.

    Raises BundleError only when there is nothing to check (no manifest, or one that cannot
    be read); every other inconsistency is returned in `problems`.
    """
    from repo_intel.compile.contracts import Manifest

    raw = source.read(["manifest.json"])["manifest.json"]
    if raw is None:
        raise bundle.BundleError(f"no bundle in {source.label}; run `rkp compile` first")
    try:
        document = json.loads(raw)
    except ValueError as error:
        raise bundle.BundleError(f"{source.label}: manifest.json is not valid JSON") from error
    if not isinstance(document, dict) or document.get("bundle_format") not in bundle.READABLE_FORMATS:
        found = document.get("bundle_format") if isinstance(document, dict) else None
        raise bundle.BundleError(f"{source.label}: manifest.json has format {found!r}; this engine reads {', '.join(sorted(bundle.READABLE_FORMATS))}")
    try:
        manifest = Manifest.model_validate(document.get("snapshot"))
    except ValueError as error:
        raise bundle.BundleError(f"{source.label}: manifest.json does not hold a valid snapshot record: {error}") from error

    problems: list[str] = []
    units = [u for u in document.get("units") or [] if isinstance(u, dict)]
    if len(units) != len(document.get("units") or []):
        problems.append("manifest.json: a unit entry is not an object")
    named = []
    for unit in units:
        file = unit.get("file")
        if not isinstance(file, str) or not _UNIT_FILE.match(file):
            problems.append(f"manifest.json: unit {unit.get('unit')!r} names {file!r}, which is not a file under units/")
        elif file in named:
            problems.append(f"manifest.json: {file} is named by more than one unit")
        else:
            named.append(file)
    data = source.read([*named, *(f"{t}.jsonl" for t in SIDE_TABLES)])
    problems += [f"{name}: a symbolic link out of the bundle; not read" for name in source.escaped]

    # Format 1 wrote every field and has no row_defaults; a damaged table is checked by verify.
    defaults = {k: v for k, v in (document.get("row_defaults") or {}).items() if isinstance(v, dict)} if isinstance(document.get("row_defaults"), dict) else {}
    table_of = {ROW_NAMES[table]: table for table in _tables()}
    shards: dict[str, dict[str, Any]] = {}
    written: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for unit in units:
        file = unit.get("file")
        if unit.get("unit") in shards:
            problems.append(f"manifest.json: unit {unit.get('unit')!r} is listed more than once")
            continue
        if file not in named:
            continue
        if data.get(file) is None:
            problems.append(f"{file}: named by the manifest but missing")
            continue
        # The base tables always; an optional table only once it has a row, as hydrate does.
        shard: dict[str, Any] = {"unit": unit.get("unit"), **{table: [] for table in bundle.ROW_TABLES}}
        written[file] = []
        unknown_tables: dict[Any, int] = {}
        for row in _parse_lines(data[file], file, problems):
            name = row.pop("table", None)
            if name not in table_of:
                unknown_tables[name] = unknown_tables.get(name, 0) + 1
                continue
            written[file].append((name, row))
            shard.setdefault(table_of[name], []).append({**json.loads(_canonical(defaults.get(name, {}))), **row})
        for name, n in sorted(unknown_tables.items(), key=lambda item: str(item[0])):
            problems.append(f"{file}: {n} {'row names' if n == 1 else 'rows name'} table {name!r}, which format {document['bundle_format']} does not have")
        shards[unit["unit"]] = shard

    side: dict[str, list[dict[str, Any]]] = {}
    for table in SIDE_TABLES:
        file = f"{table}.jsonl"
        if data.get(file) is None:
            problems.append(f"{file}: missing")
            side[table] = []
        else:
            side[table] = _parse_lines(data[file], file, problems)
    exports = _load_exports(source, document, problems)
    return {"source": source, "document": document, "manifest": manifest, "tier": document.get("tier", "full"), "units": units, "shards": shards, "written": written, "tables": side, "exports": exports, "problems": problems}


def _load_exports(source: Source, document: dict[str, Any], problems: list[str]) -> Any:
    """The parsed `exports.json`, or None when the bundle does not have one (bundles written
    before it existed). A file the manifest does not name, or one it names that is missing or
    not JSON, is a problem; whether its content matches is checked by verify."""
    entry = document.get("exports")
    raw = source.read([bundle.EXPORTS_FILE])[bundle.EXPORTS_FILE]
    if bundle.EXPORTS_FILE in source.escaped:
        problems.append(f"{bundle.EXPORTS_FILE}: a symbolic link out of the bundle; not read")
        return None
    if entry is None:
        if raw is not None:
            problems.append(f"{bundle.EXPORTS_FILE}: not named by the manifest")
        return None
    if raw is None:
        problems.append(f"{bundle.EXPORTS_FILE}: named by the manifest but missing")
        return None
    try:
        return json.loads(raw)
    except ValueError:
        problems.append(f"{bundle.EXPORTS_FILE}: not valid JSON")
        return None


# ------------------------------------------------------------------ verify


def _behaviour_problems(file: str, shard: dict[str, Any]) -> list[str]:
    """Behaviour rows the compiler would not have stored: an owner or artifact that is not an
    entity of the unit, evidence the unit does not hold, or a body that no longer validates."""
    from repo_intel.compile.behavior import validate_unit

    entities = {e.get("id"): e for e in shard["entities"] if isinstance(e, dict)}
    evidence = {e.get("id") for e in shard["evidence"] if isinstance(e, dict)}
    problems = []
    for row in shard.get("behaviors", []):
        name = f"{file}: behavior {row.get('id')}"
        owner = entities.get(row.get("owner"))
        if owner is None or owner.get("kind") not in ("Program", "Symbol"):
            problems.append(f"{name}: owner {row.get('owner')} is not a Program or Symbol of this unit")
        if row.get("artifact") not in entities:
            problems.append(f"{name}: artifact {row.get('artifact')} is not an entity of this unit")
        missing = [ev for ev in row.get("evidence_ids") or [] if ev not in evidence]
        if missing or not row.get("evidence_ids"):
            problems.append(f"{name}: cites evidence this unit does not hold" if missing else f"{name}: cites no evidence")
        ref = {"kind": owner.get("kind"), "natural_key": owner.get("natural_key"), "name": owner.get("name")} if owner else {}
        invalid = validate_unit({**row, "owner": ref}) if owner else []
        if invalid:
            problems.append(f"{name}: not a valid behaviour unit ({invalid[0]})")
    return problems


def verify_bundle(loaded: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Every way the bundle disagrees with itself or with the publish gate; and what was checked.

    1. The manifest agrees with the snapshot record it carries, and the snapshot's hashes re-derive.
    2. Row defaults and the tier are what the format and the tier say; an architecture-tier
       bundle holds no detail kind, table or attribute (bundle.DETAIL_ATTRIBUTES).
    3. Every unit the snapshot names has its file, every unit file is named, row counts match,
       and each unit's rows hash to its digest: the snapshot's in the full tier, the one
       manifest.json records in the architecture tier.
    4. The repository-level files hash to the snapshot's references.
    5. The rows, as a reader rebuilds them, pass the publish gate (repo_intel.compile.verify),
       and every behaviour row belongs to an entity of its unit and validates.
    6. `exports.json`, when the manifest names it, hashes to its digest, cites only entities
       and evidence of the bundle, and passes the secret scan that needs no source. A bundle
       written before the file existed has none, and that is not a problem.
    """
    from repo_intel.compile.ontology import functional_violations
    from repo_intel.compile.verify import content_leaves, verify_snapshot
    from repo_intel.storage.hashing import digest, merkle_root
    from repo_intel.workspace.settings import DEFAULT_REQUIRED_COVERAGE

    document, manifest, shards, side = loaded["document"], loaded["manifest"], loaded["shards"], loaded["tables"]
    problems = list(loaded["problems"])
    tier = loaded["tier"]

    for key in ("repository_id", "commit_sha", "source_tree_hash", "toolchain_hash", "override_set_hash", "rkp_hash", "status"):
        if document.get(key) != getattr(manifest, key):
            problems.append(f"manifest.json: {key} differs from the snapshot record")
    if manifest.status != "complete":
        problems.append(f"manifest.json: the snapshot's status is {manifest.status!r}, not complete")
    provenance = digest({"toolchain_hash": manifest.toolchain_hash, "ledger_entries_ref": manifest.ledger_entries_ref, "override_set_hash": manifest.override_set_hash})
    if provenance != manifest.provenance_hash:
        problems.append("manifest.json: provenance_hash does not re-derive from the toolchain, decisions and overrides")
    if digest([manifest.rkp_schema, manifest.content_hash, manifest.provenance_hash]) != manifest.rkp_hash:
        problems.append("manifest.json: rkp_hash does not re-derive from the content and provenance hashes")

    # Row defaults and tier.
    if document["bundle_format"] == bundle.FORMAT:
        if _canonical(document.get("row_defaults")) != _canonical(bundle.ROW_DEFAULTS):
            problems.append(f"manifest.json: row_defaults differ from the ones {bundle.FORMAT} fixes")
        for file, rows in sorted(loaded["written"].items()):
            spelled = sum(1 for name, row in rows if any(k in row and _canonical(row[k]) == _canonical(v) for k, v in bundle.ROW_DEFAULTS.get(name, {}).items()))
            if spelled:
                problems.append(f"{file}: {spelled} {'row spells' if spelled == 1 else 'rows spell'} out a default value, which {bundle.FORMAT} leaves out")
    units = loaded["units"]
    detail_kinds = document.get("detail_kinds")
    if tier not in bundle.TIERS:
        problems.append(f"manifest.json: tier {tier!r} is not one of {', '.join(bundle.TIERS)}")
    elif tier == "architecture":
        if detail_kinds != list(bundle.DETAIL_KINDS):
            problems.append(f"manifest.json: detail_kinds is {detail_kinds!r}; the architecture tier leaves out {list(bundle.DETAIL_KINDS)}")
        # Bundles written before behaviour existed have no detail_tables; they cannot hold any.
        # One written before a later detail table existed names fewer (['behaviors'] before
        # transform shapes): any subset is right, and the rows check below still applies.
        tables = document.get("detail_tables")
        if "detail_tables" in document and not (isinstance(tables, list) and all(isinstance(t, str) for t in tables) and set(tables) <= set(bundle.DETAIL_TABLES)):
            problems.append(f"manifest.json: detail_tables is {tables!r}; the architecture tier leaves out only tables among {list(bundle.DETAIL_TABLES)}")
        if not isinstance(document.get("detail_omitted"), dict):
            problems.append("manifest.json: an architecture-tier bundle does not say how much detail it left out")
        problems += [f"manifest.json: unit {u.get('unit')!r} has no digest; the architecture tier records one per unit" for u in units if not u.get("digest")]
        kinds = sorted({e["kind"] for s in shards.values() for e in s["entities"] if e.get("kind") in bundle.DETAIL_KINDS})
        kept = [f"{', '.join(kinds)} entities"] if kinds else []
        kept += [f"{n} {table} rows" for table in bundle.DETAIL_TABLES if (n := sum(len(s.get(table, [])) for s in shards.values()))]
        for kind, names in sorted(bundle.DETAIL_ATTRIBUTES.items()):
            for name in names:
                n = sum(1 for s in shards.values() for e in s["entities"] if e.get("kind") == kind and isinstance(e.get("attributes"), dict) and name in e["attributes"])
                if n:
                    kept.append(f"{n} {kind} {'entity' if n == 1 else 'entities'} with attributes.{name}")
        if kept:
            problems.append(f"the architecture tier holds detail it should leave out: {'; '.join(kept)}")
    elif detail_kinds is not None or "detail_tables" in document or "detail_omitted" in document:
        problems.append("manifest.json: a full bundle names detail kinds it left out")

    # Units against the snapshot, row counts and digests. A unit entry or the counts written
    # before an optional table existed do not name it; that reads as no rows.
    expected = {ref.unit: ref.digest for ref in manifest.shards}
    by_unit = {u.get("unit"): u for u in units}
    problems += [f"manifest.json: the snapshot's unit {unit!r} has no unit file" for unit in sorted(expected.keys() - by_unit.keys())]
    problems += [f"manifest.json: unit {unit!r} is not in the snapshot" for unit in sorted(by_unit.keys() - expected.keys(), key=str)]
    named = {u.get("file") for u in units}
    problems += [f"{file}: not named by the manifest" for file in loaded["source"].unit_files() if file not in named]
    optional = set(bundle.OPTIONAL_TABLES)
    totals = {table: 0 for table in _tables()}
    shard_digests = {}
    for unit, shard in sorted(shards.items()):
        entry = by_unit[unit]
        for table in _tables():
            n = len(shard.get(table, []))
            totals[table] += n
            if entry.get(table, 0 if table in optional else None) != n:
                problems.append(f"{entry['file']}: {n} {table} rows; the manifest says {entry.get(table)}")
        shard_digests[unit] = digest(shard)
        want = entry.get("digest") if tier == "architecture" else expected.get(unit)
        if want and shard_digests[unit] != want:
            problems.append(f"{entry['file']}: rows do not hash to the unit's digest in {'manifest.json' if tier == 'architecture' else 'the snapshot record'}")
        problems += _behaviour_problems(entry["file"], shard)
    counts = document.get("counts") or {}
    files = side["files"]
    actual = {**totals, "coverage": len(side["coverage"]), "conflicts": len(side["conflicts"]), "repository_unknowns": len(side["unknowns"]), "decisions": len(side["decisions"]),
              "files": sum(1 for f in files if "blob_sha256" in f), "excluded_files": sum(1 for f in files if "excluded" in f)}
    problems += [f"manifest.json: counts.{key} is {counts.get(key)}; the files hold {n}" for key, n in actual.items() if counts.get(key, 0 if key in optional else None) != n]

    # Repository-level files. Coverage and conflicts are checked by the publish gate below.
    if digest(side["unknowns"]) != manifest.unknowns_ref:
        problems.append("unknowns.jsonl: rows do not hash to the snapshot's unknowns_ref")
    if digest([{"decision_id": d.get("decision_id"), "digest": digest(d)} for d in side["decisions"]]) != manifest.ledger_entries_ref:
        problems.append("decisions.jsonl: rows do not hash to the snapshot's ledger_entries_ref")
    malformed = [f for f in files if not isinstance(f.get("path"), str) or ("blob_sha256" in f) == ("excluded" in f) or ("blob_sha256" in f and not isinstance(f.get("size"), int))]
    if malformed:
        problems.append(f"files.jsonl: {len(malformed)} rows are neither a read file (path, blob_sha256, size) nor an excluded one (path, excluded)")

    # The publish gate, on the rows as a reader rebuilds them.
    full = tier == "full"
    decisions = side["decisions"]
    path_manifest = {"entries": [f for f in files if f not in malformed and "blob_sha256" in f]}
    content_hash = manifest.content_hash if full else merkle_root(content_leaves(shard_digests, digest(side["coverage"]), digest(side["conflicts"])))
    try:
        gate = verify_snapshot(
            repository_id=manifest.repository_id, path_manifest=path_manifest, shards=shards, shard_digests=shard_digests,
            coverage=side["coverage"], coverage_ref=manifest.coverage_ref, conflicts=side["conflicts"], conflicts_ref=manifest.conflicts_ref,
            content_hash=content_hash, required_coverage=DEFAULT_REQUIRED_COVERAGE,
            hypothesis_decisions={d["decision_id"] for d in decisions if d.get("hypotheses") and d.get("decision_id")},
        )
    except (KeyError, TypeError, AttributeError) as error:  # rows too damaged to check one by one
        gate = [f"the publish gate could not read the rows ({type(error).__name__}: {error})"]
    if not full:
        # A conflict lists every assertion of a functional violation, detail included; the
        # architecture tier keeps only some of them, which still belong to that conflict.
        recorded = [frozenset(c.get("assertion_ids", [])) for c in side["conflicts"] if c.get("conflict_type") == "functional_violation"]
        every = [a for s in shards.values() for a in s["assertions"] if isinstance(a, dict) and "predicate" in a]
        for (predicate, subject), rows in functional_violations(every).items():
            if any(frozenset(a["id"] for a in rows) <= ids for ids in recorded):
                message = f"{predicate} on {subject}: {len(rows)} objects for a single-valued predicate and no conflict recorded"
                gate = [p for p in gate if p != message]
    problems += [f"publish gate: {p}" for p in gate]
    known = {d.get("decision_id") for d in decisions}
    cited = {a["decision_ref"] for s in shards.values() for a in s["assertions"] if isinstance(a, dict) and a.get("decision_ref")}
    cited |= {e["attributes"][k] for s in shards.values() for e in s["entities"] if isinstance(e, dict) and isinstance(e.get("attributes"), dict) for k in DECISION_ATTRIBUTES if e["attributes"].get(k)}
    problems += [f"publish gate: decision {d} is cited but not in decisions.jsonl" for d in sorted(cited - known)]
    problems += _exports_problems(loaded)

    checked = {
        "bundle_format": document["bundle_format"], "tier": tier, "repository_id": manifest.repository_id, "commit_sha": manifest.commit_sha, "rkp_hash": manifest.rkp_hash,
        "units": len(units), **totals, "files": actual["files"],
        "content_hash": "re-derived from the rows" if full else "not re-derivable: the architecture tier leaves detail out, so each unit is checked against its own digest",
        "exports": {name: len(loaded["exports"][name]) for name in bundle.EXPORT_LISTS} if isinstance(loaded.get("exports"), dict) and bundle.exports_problem(loaded["exports"]) is None
        else "absent: the bundle was written before exports.json existed" if (document.get("exports") is None) else "unreadable",
    }
    return problems, checked


_METHOD = re.compile(r"^[A-Z]{3,7}$")


def _exports_problems(loaded: dict[str, Any]) -> list[str]:
    """`exports.json` against manifest.json and the rows: its digest and counts, its shape,
    that every entity and evidence it cites is in the bundle (in either tier: the writer keeps
    the evidence the exports cite), that a method is an upper-case HTTP method, and the
    secret scan that needs no source (repo_intel.ingest.publish_secrets.export_findings)."""
    from repo_intel.ingest.publish_secrets import export_findings
    from repo_intel.storage.hashing import digest

    entry, exports = loaded["document"].get("exports"), loaded.get("exports")
    if entry is None or exports is None:
        return []  # no exports (an older bundle), or the file problem load_bundle reported
    name = bundle.EXPORTS_FILE
    problems = []
    if not isinstance(entry, dict) or entry.get("file") != name:
        problems.append(f"manifest.json: exports must name {{\"file\": \"{name}\", \"digest\", {', '.join(bundle.EXPORT_LISTS)}}}")
        entry = {}
    shape = bundle.exports_problem(exports)
    if shape is not None:
        return problems + [f"{name}: {shape}"]
    if digest(exports) != entry.get("digest"):
        problems.append(f"{name}: does not hash to the digest in manifest.json")
    for group in bundle.EXPORT_LISTS:
        if entry.get(group) != len(exports[group]):
            problems.append(f"{name}: {len(exports[group])} {group}; the manifest says {entry.get(group)}")
    entities = {e.get("id") for s in loaded["shards"].values() for e in s["entities"] if isinstance(e, dict)}
    evidence = {e.get("id") for s in loaded["shards"].values() for e in s["evidence"] if isinstance(e, dict)}
    for group, fields in (("operations", ("interface", "component")), ("unresolved_calls", ("caller",))):
        for number, row in enumerate(exports[group]):
            where = f"{name}: {group}[{number}]"
            for field in fields:
                if row.get(field) is not None and row[field] not in entities:
                    problems.append(f"{where}: {field} {row[field]} is not an entity of the bundle")
            ids = row.get("evidence_ids")
            if not isinstance(ids, list) or not ids:
                problems.append(f"{where}: cites no evidence")
            elif missing := [ev for ev in ids if ev not in evidence]:
                problems.append(f"{where}: cites evidence the bundle does not hold ({', '.join(map(str, missing[:3]))})")
            if "method" in row and not (isinstance(row["method"], str) and _METHOD.match(row["method"])):
                problems.append(f"{where}: method {row['method']!r} is not an upper-case HTTP method")
            if not isinstance(row.get("path" if group == "unresolved_calls" else "route"), str):
                problems.append(f"{where}: has no {'path' if group == 'unresolved_calls' else 'route'}")
    problems += [f"publish gate: {name}: {finding}" for finding in export_findings(exports)]
    return problems


# ------------------------------------------------------------------ diff


def _entities(loaded: dict[str, Any], leave_out: tuple[str, ...], leave_out_attributes: dict[str, tuple[str, ...]] | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Entities by (kind, natural key), merged across units as the engine merges them; and id -> key.

    Keyed by kind and natural key rather than ID, so two repositories' bundles compare too
    (an entity ID folds in the repository ID). Attributes named in `leave_out_attributes`
    for an entity's kind are dropped, as the architecture tier drops them.
    """
    out: dict[str, dict[str, Any]] = {}
    key_of: dict[str, str] = {}
    for shard in loaded["shards"].values():
        for e in shard["entities"]:
            if e["kind"] in leave_out:
                continue
            dropped = (leave_out_attributes or {}).get(e["kind"], ())
            if any(name in e["attributes"] for name in dropped):
                e = {**e, "attributes": {k: v for k, v in e["attributes"].items() if k not in dropped}}
            key = _canonical([e["kind"], e["natural_key"]])
            key_of[e["id"]] = key
            old = out.get(key)
            out[key] = e if old is None else {**e, "attributes": {**old["attributes"], **e["attributes"]}, "evidence_ids": sorted(set(old["evidence_ids"]) | set(e["evidence_ids"]))}
    return out, key_of


def _claims(loaded: dict[str, Any], key_of: dict[str, str], remap: dict[str, str]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    out = {}
    for shard in loaded["shards"].values():
        for a in shard["assertions"]:
            if a["subject"] not in key_of or a["object"] not in key_of:
                continue  # touches a kind left out of the comparison
            subject, obj = key_of[a["subject"]], key_of[a["object"]]
            out[(remap.get(subject, subject), a["predicate"], remap.get(obj, obj), _canonical(a.get("qualifiers", {})))] = a
    return out


def _without_spans(value: Any) -> Any:
    """A behaviour body with every `at` span removed: an edit above a function moves its spans
    without changing what it does."""
    if isinstance(value, dict):
        return {k: _without_spans(v) for k, v in value.items() if k != "at"}
    if isinstance(value, list):
        return [_without_spans(v) for v in value]
    return value


def _behaviours(loaded: dict[str, Any], key_of: dict[str, str], remap: dict[str, str]) -> dict[str, list[str]]:
    """Each owner's behaviour units, by the owner's (kind, natural key), as sorted canonical text."""
    out: dict[str, list[str]] = {}
    for shard in loaded["shards"].values():
        for b in shard.get("behaviors", []):
            key = key_of.get(b.get("owner"))
            if key is None:
                continue
            unit = {k: b.get(k) for k in ("kind", "language", "params", "body")}
            out.setdefault(remap.get(key, key), []).append(_canonical(_without_spans(unit)))
    return {k: sorted(v) for k, v in out.items()}


# How an exports row is matched across two bundles: an operation by its interface, method and
# route; an unresolved call by its caller, method and path. The entity is compared by kind and
# natural key, as entities are, so two repositories' exports compare too.
EXPORT_KEYS = {"operations": ("interface", "method", "route"), "unresolved_calls": ("caller", "method", "path")}


def _exports_by_key(loaded: dict[str, Any], key_of: dict[str, str], remap: dict[str, str]) -> dict[str, dict[tuple[str, ...], dict[str, Any]]] | None:
    """Each exports list's rows by their match key (EXPORT_KEYS); None when the bundle has no
    readable exports.json (written before it existed, or damaged: verify reports that)."""
    exports = loaded.get("exports")
    if not isinstance(exports, dict) or bundle.exports_problem(exports) is not None:
        return None
    out: dict[str, dict[tuple[str, ...], dict[str, Any]]] = {}
    for group, (entity, *fields) in EXPORT_KEYS.items():
        rows: dict[tuple[str, ...], dict[str, Any]] = {}
        for row in exports[group]:
            key = key_of.get(row.get(entity)) if isinstance(row.get(entity), str) else None
            ref = remap.get(key, key) if key is not None else _canonical(row.get(entity))
            rows[(ref, *(_canonical(row.get(f)) for f in fields))] = row
        out[group] = rows
    return out


def coverage_changes(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, Any]:
    """Coverage entries added, removed or changed, by domain and scope, with domain totals.

    `platform_constructs` entries also say which constructs' counts changed, which were
    seen for the first time, and which are no longer seen.
    """
    def by_scope(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        return {(r.get("domain", ""), r.get("scope", "")): r for r in rows}

    def totals(rows: list[dict[str, Any]]) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for r in rows:
            t = out.setdefault(r.get("domain", ""), [0, 0])
            t[0] += r.get("numerator", 0)
            t[1] += r.get("denominator", 0)
        return out

    old, new = by_scope(before), by_scope(after)
    changes = []
    for key in sorted(old.keys() | new.keys()):
        a, b = old.get(key), new.get(key)
        ratio = lambda r: None if r is None else {"numerator": r.get("numerator", 0), "denominator": r.get("denominator", 0)}
        if a is not None and b is not None and _canonical(a) == _canonical(b):
            continue
        row: dict[str, Any] = {"domain": key[0], "scope": key[1], "change": "added" if a is None else "removed" if b is None else "changed", "before": ratio(a), "after": ratio(b)}
        if key[0] == "platform_constructs":
            ca, cb = (r.get("detail", {}).get("constructs", {}) if r else {} for r in (a, b))
            row["constructs"] = {c: {"before": ca.get(c), "after": cb.get(c)} for c in sorted(ca.keys() | cb.keys()) if ca.get(c) != cb.get(c)}
            na, nb = (set(r.get("detail", {}).get("not_seen", [])) if r else set() for r in (a, b))
            row["now_seen"] = sorted((na - nb) & set(cb))
            row["no_longer_seen"] = sorted(set(ca) - set(cb))
        changes.append(row)
    ta, tb = totals(before), totals(after)
    domains = [{"domain": d, "before": {"numerator": ta[d][0], "denominator": ta[d][1]} if d in ta else None, "after": {"numerator": tb[d][0], "denominator": tb[d][1]} if d in tb else None}
               for d in sorted(ta.keys() | tb.keys()) if ta.get(d) != tb.get(d)]
    return {"domains": domains, "entries": changes}


def diff_bundles(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Structural difference from bundle `a` (older) to `b` (newer), both from `load_bundle`.

    Entities are matched by kind and natural key; an entity whose key changed counts as moved
    when repo_intel.knowledge.delta.match_moves links the two (same kind and name, and shared
    content-derived evidence). Relations are compared as claims (subject, predicate, object,
    qualifiers), with moved entities mapped onto their new keys, as the snapshot delta does.
    Behaviour is compared per owner, spans left out. When one bundle is architecture tier and
    the other full, both are compared at the architecture tier, so function-level detail
    (Symbols, behaviour and bundle.DETAIL_ATTRIBUTES) does not show as added or removed.
    The exports (`exports.json`) are compared row by row (EXPORT_KEYS) when both bundles
    have them: operations and unresolved calls added and removed.

    The snapshot delta itself (delta.knowledge_delta) is not used: it needs the toolchain and
    path manifests from the cache, which a bundle does not carry, it refuses two repositories,
    and it truncates its rows.
    """
    from repo_intel.knowledge.delta import COMPILE_ARTEFACTS, PLANE_OF, match_moves

    tier = "architecture" if "architecture" in (a["tier"], b["tier"]) else "full"
    leave_out = tuple(bundle.DETAIL_KINDS) if tier == "architecture" else ()
    leave_out_attributes = bundle.DETAIL_ATTRIBUTES if tier == "architecture" else {}
    ents_a, key_a = _entities(a, leave_out, leave_out_attributes)
    ents_b, key_b = _entities(b, leave_out, leave_out_attributes)
    removed = {k: e for k, e in ents_a.items() if k not in ents_b}
    added = {k: e for k, e in ents_b.items() if k not in ents_a}
    moves = match_moves(removed, added)

    def brief(e: dict[str, Any], change: str, **extra: Any) -> dict[str, Any]:
        return {"change": change, "kind": e["kind"], "name": e["name"], "id": e["id"], "natural_key": e["natural_key"], "plane": PLANE_OF.get(e["kind"]), **extra}

    rows = [brief(ents_b[new], "moved", previous_id=ents_a[old]["id"], from_key=ents_a[old]["natural_key"], basis=basis) for old, (new, basis) in moves.items()]
    moved_to = {new for new, _ in moves.values()}
    rows += [brief(e, "added") for k, e in added.items() if k not in moved_to]
    rows += [brief(e, "removed") for k, e in removed.items() if k not in moves]
    evidence_only = 0
    for key in ents_a.keys() & ents_b.keys():
        before, after = ents_a[key], ents_b[key]
        # Decision references change whenever a decision's inputs do; not a difference in themselves.
        compared = (set(before["attributes"]) | set(after["attributes"])) - COMPILE_ARTEFACTS - set(DECISION_ATTRIBUTES)
        fields = sorted(f"attributes.{k}" for k in compared if _canonical(before["attributes"].get(k)) != _canonical(after["attributes"].get(k)))
        if before["name"] != after["name"]:
            fields.insert(0, "name")
        if fields:
            rows.append(brief(after, "changed", changed_fields=fields))
        elif before["evidence_ids"] != after["evidence_ids"]:
            evidence_only += 1  # the defining file was edited; whether this entity changed is not known at that grain
    rows.sort(key=lambda r: (r["kind"], r["name"], r["change"], r["id"]))
    by_kind: dict[str, dict[str, int]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], {"added": 0, "removed": 0, "changed": 0, "moved": 0})[r["change"]] += 1

    remap = {old: new for old, (new, _) in moves.items()}
    claims_a, claims_b = _claims(a, key_a, remap), _claims(b, key_b, {})
    names = {**{k: e for k, e in ents_a.items()}, **ents_b}
    for old, new in remap.items():
        names.setdefault(old, ents_b[new])

    def end(key: str) -> dict[str, Any]:
        e = names[key]
        return {"kind": e["kind"], "name": e["name"], "id": e["id"]}

    support = lambda x: {"epistemic_status": x.get("epistemic_status"), "evidence_class": x.get("evidence_class"), "resolution": x.get("resolution")}
    relations = [{"change": "added", "predicate": k[1], "subject": end(k[0]), "object": end(k[2])} for k in claims_b.keys() - claims_a.keys()]
    relations += [{"change": "removed", "predicate": k[1], "subject": end(k[0]), "object": end(k[2])} for k in claims_a.keys() - claims_b.keys()]
    relations += [{"change": "support_changed", "predicate": k[1], "subject": end(k[0]), "object": end(k[2]), "before": support(claims_a[k]), "after": support(claims_b[k])}
                  for k in claims_a.keys() & claims_b.keys() if support(claims_a[k]) != support(claims_b[k])]
    relations.sort(key=lambda r: (r["predicate"], r["subject"]["name"], r["object"]["name"], r["change"]))
    by_predicate: dict[str, dict[str, int]] = {}
    for r in relations:
        by_predicate.setdefault(r["predicate"], {"added": 0, "removed": 0, "support_changed": 0})[r["change"]] += 1

    behaviours: list[dict[str, Any]] = []
    if tier == "full":
        bhv_a, bhv_b = _behaviours(a, key_a, remap), _behaviours(b, key_b, {})
        for key in sorted(bhv_a.keys() | bhv_b.keys()):
            before_units, after_units = bhv_a.get(key, []), bhv_b.get(key, [])
            if before_units == after_units:
                continue
            change = "added" if not before_units else "removed" if not after_units else "changed"
            behaviours.append({"change": change, "owner": end(key), "units_before": len(before_units), "units_after": len(after_units)})
        behaviours.sort(key=lambda r: (r["owner"]["kind"], r["owner"]["name"], r["change"], r["owner"]["id"]))

    # exports.json: compared only when both bundles have it, so an older bundle on one side
    # does not show every export as added or removed.
    exp_a, exp_b = _exports_by_key(a, key_a, remap), _exports_by_key(b, key_b, {})
    exports: dict[str, Any] = {"compared": exp_a is not None and exp_b is not None, "summary": {"added": 0, "removed": 0}, **{group: [] for group in EXPORT_KEYS}}
    if exp_a is not None and exp_b is not None:
        def entity_of(entity_id: Any, ents: dict[str, dict[str, Any]], key_of: dict[str, str]) -> dict[str, Any] | None:
            e = ents.get(key_of.get(entity_id, "")) if isinstance(entity_id, str) else None
            return {"kind": e["kind"], "name": e["name"], "id": e["id"]} if e else None if entity_id is None else {"id": entity_id}

        for group, (entity, *fields) in EXPORT_KEYS.items():
            for change, keys, side_rows, ents, key_of in (("added", exp_b[group].keys() - exp_a[group].keys(), exp_b[group], ents_b, key_b),
                                                          ("removed", exp_a[group].keys() - exp_b[group].keys(), exp_a[group], ents_a, key_a)):
                for k in keys:
                    row = side_rows[k]
                    exports[group].append({"change": change, entity: entity_of(row.get(entity), ents, key_of), **{f: row.get(f) for f in fields}})
                    exports["summary"][change] += 1
            exports[group].sort(key=lambda r: (_canonical(r[fields[1]]), _canonical(r["method"]), r["change"], _canonical(r[entity])))

    files_a = {f["path"]: f.get("blob_sha256") for f in a["tables"]["files"] if "blob_sha256" in f and "path" in f}
    files_b = {f["path"]: f.get("blob_sha256") for f in b["tables"]["files"] if "blob_sha256" in f and "path" in f}
    count = lambda items, change: sum(1 for r in items if r["change"] == change)

    def side(loaded: dict[str, Any]) -> dict[str, Any]:
        m = loaded["manifest"]
        return {"bundle": loaded["source"].label, "repository_id": m.repository_id, "commit_sha": m.commit_sha, "rkp_hash": m.rkp_hash, "tier": loaded["tier"]}

    counts_a, counts_b = a["document"].get("counts") or {}, b["document"].get("counts") or {}
    return {
        "from": side(a), "to": side(b),
        "same_repository": a["manifest"].repository_id == b["manifest"].repository_id,
        "identical": a["manifest"].content_hash == b["manifest"].content_hash,
        "compared_tier": tier,
        "entities": {"summary": {c: count(rows, c) for c in ("added", "removed", "changed", "moved")} | {"evidence_only": evidence_only}, "by_kind": dict(sorted(by_kind.items())), "rows": rows},
        "relations": {"summary": {c: count(relations, c) for c in ("added", "removed", "support_changed")}, "by_predicate": dict(sorted(by_predicate.items())), "rows": relations},
        "behaviors": {"compared": tier == "full", "summary": {c: count(behaviours, c) for c in ("added", "removed", "changed")}, "rows": behaviours},
        "exports": exports,
        "coverage": coverage_changes(a["tables"]["coverage"], b["tables"]["coverage"]),
        "counts": {k: {"before": counts_a.get(k), "after": counts_b.get(k)} for k in ("conflicts", "repository_unknowns", "decisions") if counts_a.get(k) != counts_b.get(k)},
        "files": {"added": sorted(files_b.keys() - files_a.keys()), "removed": sorted(files_a.keys() - files_b.keys()), "modified": sorted(p for p in files_a.keys() & files_b.keys() if files_a[p] != files_b[p])},
        "warnings": [f"{loaded['source'].label}: {p}" for loaded in (a, b) for p in loaded["problems"]],
    }
