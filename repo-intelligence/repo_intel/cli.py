"""Command-line engine: compile a repository into its `.rkp/` knowledge bundle.

    rkp compile [PATH] [--ref REF] [--working-tree] [--json]
    rkp status  [PATH] [--ref REF] [--working-tree] [--json]

    rkp serve   [PATH] --stdio

    rkp verify  [PATH] [--json]
    rkp diff    A B [--repo PATH] [--json]

`verify` re-checks the committed `.rkp/knowledge/` without compiling; `diff` compares two
bundles, read from two directories or from two commits of one repository (see `_verify`
and `_diff` below). `serve --stdio` answers the Explorer's read API from the bundle, one JSON request per
line (repo_intel.knowledge.local_api). `--working-tree` analyses the files on disk, uncommitted changes included, on top of
`--ref`. Nothing is written to `.git`; the source tree hash is the one the same files
will have once committed, so the bundle stays fresh across that commit.

No server, port or database. Editor shells (the VS Code extension) run this as a
child process with `--json`: every line on stdout is one JSON object,
`{"event": "progress", "stage": ..., ...}` while working, then exactly one
`{"event": "result", ...}` or `{"event": "error", "message": ...}`. Exit status is
0 on success, 1 on an error the engine reports, 2 on bad arguments.

Decision models are off unless `--models` is given; then the providers come from
the environment as for the server (`Gateway.from_env` with `RKP_ALLOW_PROVIDERS`).
Without models, ambiguous decisions abstain, and the bundle says so.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from repo_intel.knowledge import bundle


def _gateway(enabled: bool) -> Any:
    if not enabled:
        return None
    from repo_intel.decisions.gateway import Gateway
    allowed = frozenset(p.strip() for p in os.environ.get("RKP_ALLOW_PROVIDERS", "").split(",") if p.strip())
    return Gateway.from_env(allowed_providers=allowed)


def _emit(event: str, **fields: Any) -> None:
    sys.stdout.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")
    sys.stdout.flush()


def _print_status(status: dict[str, Any]) -> None:
    print(f"{status['state']}: {status['repository']} at {status['commit_sha'][:12]}")
    for reason in status["reasons"]:
        print(f"  {reason}")
    if status["uncommitted_changes"] and not status["working_tree"]:
        print("  uncommitted changes are not analysed; use --working-tree to include them")


def _solution_parser(commands: Any) -> None:
    solution = commands.add_parser("solution", help="check a solution's members and link entities across them")
    actions = solution.add_subparsers(dest="action", required=True)
    for name, text in (("check", "validate .rkp/solution.json and open every member's bundle"), ("links", "print identity links, candidates and unmatched references")):
        action = actions.add_parser(name, help=text)
        action.add_argument("path", nargs="?", default=".", help="the directory holding .rkp/solution.json (default: .)")
        action.add_argument("--json", action="store_true", help="one JSON object on stdout")


def _solution(args: Any) -> int:
    """`rkp solution check|links [PATH] [--json]` (repo_intel.solution).

    Reads `.rkp/solution.json` under PATH and the members' bundles; compiles nothing.
    Exit status 0 when every member and input was read, 1 otherwise or on an invalid
    manifest. With --json, one `{"event": "result", ...}` or `{"event": "error", ...}` line.
    """
    from repo_intel.solution import commands as solution_commands

    try:
        report = solution_commands.check(args.path) if args.action == "check" else solution_commands.links(args.path)
    except Exception as error:  # reported, never a traceback on an editor's stdout
        message = str(error) or type(error).__name__
        if args.json:
            _emit("error", message=message, kind=type(error).__name__)
        else:
            print(f"rkp: {message}", file=sys.stderr)
        return 1
    if args.json:
        _emit("result", **report)
    else:
        lines = solution_commands.format_check(report) if args.action == "check" else solution_commands.format_links(report)
        print("\n".join(lines))
    return 1 if report["problems"] else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["__step_worker__"]:
        # The frozen engine is its own step worker (repo_intel.compile.buildgraph.worker_command).
        from repo_intel.compile import step_worker
        return step_worker.main(argv[1:])
    parser = argparse.ArgumentParser(prog="rkp", description="Compile a repository into its .rkp/ knowledge bundle.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (("compile", "compile a commit and write .rkp/knowledge/"), ("status", "say whether .rkp/knowledge/ describes a commit")):
        command = commands.add_parser(name, help=text)
        command.add_argument("path", nargs="?", default=".", help="a directory inside the repository (default: .)")
        command.add_argument("--ref", default="HEAD", help="commit, branch or tag (default: HEAD)")
        command.add_argument("--working-tree", action="store_true", help="analyse the files on disk, uncommitted changes included")
        command.add_argument("--json", action="store_true", help="JSON lines on stdout, for editor shells")
        command.add_argument("--models", action="store_true", help="allow decision models configured in the environment")
    serve = commands.add_parser("serve", help="answer the Explorer's API from .rkp/knowledge/ over stdin and stdout")
    serve.add_argument("path", nargs="?", default=".", help="a directory inside the repository (default: .)")
    serve.add_argument("--stdio", action="store_true", required=True, help="JSON lines on stdin and stdout (the only transport)")
    _solution_parser(commands)
    _add_verify_command(commands)
    _add_diff_command(commands)
    args = parser.parse_args(argv)
    if args.command == "solution":
        return _solution(args)
    if args.command == "verify":
        return _verify(args)
    if args.command == "diff":
        return _diff(args)
    if args.command == "serve":
        from repo_intel.knowledge import local_api
        try:
            return local_api.serve_stdio(args.path, log=lambda text: print(f"rkp serve: {text}", file=sys.stderr))
        except bundle.BundleError as error:
            print(f"rkp: {error}", file=sys.stderr)
            return 1

    def progress(stage: str, detail: dict[str, Any]) -> None:
        if args.json:
            _emit("progress", stage=stage, **detail)

    try:
        gateway = _gateway(args.models)
        if args.command == "compile":
            if args.json:
                _emit("progress", stage="start", path=os.path.abspath(args.path), ref=args.ref, working_tree=args.working_tree)
            document = bundle.compile_bundle(args.path, args.ref, working_tree=args.working_tree, gateway=gateway, progress=progress)
            summary = {key: document[key] for key in ("repository_id", "commit_sha", "working_tree", "rkp_hash", "status", "counts")}
            if args.json:
                _emit("result", **summary)
            else:
                counts = document["counts"]
                print(f"{document['status']}: {document['repository_id']} at {document['commit_sha'][:12]}{' with uncommitted changes' if document['working_tree'] else ''}")
                print(f"  {counts['entities']} entities, {counts['assertions']} assertions, {counts['conflicts']} conflicts, {counts['unknowns'] + counts['repository_unknowns']} unknowns")
                print(f"  written to {bundle.repository_root(args.path) / bundle.BUNDLE / 'knowledge'}")
        else:
            status = bundle.bundle_status(args.path, args.ref, working_tree=args.working_tree, gateway=gateway)
            if args.json:
                _emit("result", **status)
            else:
                _print_status(status)
        return 0
    except Exception as error:  # reported, never a traceback on an editor's stdout
        message = str(error) or type(error).__name__
        if args.json:
            _emit("error", message=message, kind=type(error).__name__)
        else:
            print(f"rkp: {message}", file=sys.stderr)
        return 1


# ------------------------------------------------------------------ reading a bundle without compiling
#
# `rkp verify` and `rkp diff` read a bundle's files as they are: from a directory, or from a
# commit through `git cat-file`, never through the local cache (a cache that already holds
# an object would hide a damaged file). Problems are collected, not raised, so one run lists
# all of them.

_UNIT_FILE = re.compile(r"^units/[A-Za-z0-9._-]+\.jsonl$")
_ROW_TABLES = (("entities", "entity"), ("assertions", "assertion"), ("evidence", "evidence"), ("unknowns", "unknown"))
_SIDE_TABLES = ("coverage", "conflicts", "unknowns", "decisions", "files")
_DECISION_ATTRIBUTES = ("decision_ref", "topology_decision_ref", "primary_language_decision_ref")


def _canonical(value: Any) -> str:
    # Compared as JSON text: in Python 0 == False and 1 == 1.0, which JSON tells apart.
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class _DirectorySource:
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


class _GitSource:
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


def _bundle_directory(path: str) -> Path | None:
    """The `knowledge/` directory for a path naming a repository, its `.rkp/` or `knowledge/` itself."""
    base = Path(path)
    for candidate in (base / bundle.BUNDLE / "knowledge", base / "knowledge", base):
        if (candidate / "manifest.json").is_file():
            return candidate
    return None


def _directory_source(path: str) -> _DirectorySource:
    knowledge = _bundle_directory(path)
    if knowledge is None:
        knowledge = bundle.repository_root(path) / bundle.BUNDLE / "knowledge"
    return _DirectorySource(knowledge, str(knowledge.parent.parent if knowledge.parent.name == bundle.BUNDLE else knowledge))


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


def _load_bundle(source: _DirectorySource | _GitSource) -> dict[str, Any]:
    """Read a bundle's manifest and rows and rebuild each unit's shard as `hydrate` would.

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
    data = source.read([*named, *(f"{t}.jsonl" for t in _SIDE_TABLES)])
    problems += [f"{name}: a symbolic link out of the bundle; not read" for name in source.escaped]

    # Format 1 wrote every field and has no row_defaults; a damaged table is checked by verify.
    defaults = {k: v for k, v in (document.get("row_defaults") or {}).items() if isinstance(v, dict)} if isinstance(document.get("row_defaults"), dict) else {}
    tables = {name: table for table, name in _ROW_TABLES}
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
        shard: dict[str, Any] = {"unit": unit.get("unit"), **{table: [] for table, _ in _ROW_TABLES}}
        written[file] = []
        for row in _parse_lines(data[file], file, problems):
            name = row.pop("table", None)
            if name not in tables:
                problems.append(f"{file}: a row names table {name!r}")
                continue
            written[file].append((name, row))
            shard[tables[name]].append({**json.loads(_canonical(defaults.get(name, {}))), **row})
        shards[unit["unit"]] = shard

    side: dict[str, list[dict[str, Any]]] = {}
    for table in _SIDE_TABLES:
        file = f"{table}.jsonl"
        if data.get(file) is None:
            problems.append(f"{file}: missing")
            side[table] = []
        else:
            side[table] = _parse_lines(data[file], file, problems)
    return {"source": source, "document": document, "manifest": manifest, "tier": document.get("tier", "full"), "units": units, "shards": shards, "written": written, "tables": side, "problems": problems}


# ------------------------------------------------------------------ rkp verify


def _add_verify_command(commands: Any) -> None:
    verify = commands.add_parser("verify", help="re-check .rkp/knowledge/ against its digests and the publish gate, without compiling")
    verify.add_argument("path", nargs="?", default=".", help="a directory inside the repository, its .rkp/, or a knowledge/ directory (default: .)")
    verify.add_argument("--json", action="store_true", help="one JSON object on stdout")


def _verify_bundle(loaded: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Every way the bundle disagrees with itself or with the publish gate; and what was checked.

    1. The manifest agrees with the snapshot record it carries, and the snapshot's hashes re-derive.
    2. Row defaults and the tier are what the format and the tier say.
    3. Every unit the snapshot names has its file, every unit file is named, row counts match,
       and each unit's rows hash to its digest: the snapshot's in the full tier, the one
       manifest.json records in the architecture tier.
    4. The repository-level files hash to the snapshot's references.
    5. The rows, as a reader rebuilds them, pass the publish gate (repo_intel.compile.verify).
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
        if not isinstance(document.get("detail_omitted"), dict):
            problems.append("manifest.json: an architecture-tier bundle does not say how much detail it left out")
        problems += [f"manifest.json: unit {u.get('unit')!r} has no digest; the architecture tier records one per unit" for u in units if not u.get("digest")]
        kept = sorted({e["kind"] for s in shards.values() for e in s["entities"] if e.get("kind") in bundle.DETAIL_KINDS})
        if kept:
            problems.append(f"the architecture tier holds detail it should leave out: {', '.join(kept)} entities")
    elif detail_kinds is not None or "detail_omitted" in document:
        problems.append("manifest.json: a full bundle names detail kinds it left out")

    # Units against the snapshot, row counts and digests.
    expected = {ref.unit: ref.digest for ref in manifest.shards}
    by_unit = {u.get("unit"): u for u in units}
    problems += [f"manifest.json: the snapshot's unit {unit!r} has no unit file" for unit in sorted(expected.keys() - by_unit.keys())]
    problems += [f"manifest.json: unit {unit!r} is not in the snapshot" for unit in sorted(by_unit.keys() - expected.keys(), key=str)]
    named = {u.get("file") for u in units}
    problems += [f"{file}: not named by the manifest" for file in loaded["source"].unit_files() if file not in named]
    totals = {table: 0 for table, _ in _ROW_TABLES}
    shard_digests = {}
    for unit, shard in sorted(shards.items()):
        entry = by_unit[unit]
        for table, _ in _ROW_TABLES:
            totals[table] += len(shard[table])
            if entry.get(table) != len(shard[table]):
                problems.append(f"{entry['file']}: {len(shard[table])} {table} rows; the manifest says {entry.get(table)}")
        shard_digests[unit] = digest(shard)
        want = entry.get("digest") if tier == "architecture" else expected.get(unit)
        if want and shard_digests[unit] != want:
            problems.append(f"{entry['file']}: rows do not hash to the unit's digest in {'manifest.json' if tier == 'architecture' else 'the snapshot record'}")
    counts = document.get("counts") or {}
    files = side["files"]
    actual = {**totals, "coverage": len(side["coverage"]), "conflicts": len(side["conflicts"]), "repository_unknowns": len(side["unknowns"]), "decisions": len(side["decisions"]),
              "files": sum(1 for f in files if "blob_sha256" in f), "excluded_files": sum(1 for f in files if "excluded" in f)}
    problems += [f"manifest.json: counts.{key} is {counts.get(key)}; the files hold {n}" for key, n in actual.items() if counts.get(key) != n]

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
    cited |= {e["attributes"][k] for s in shards.values() for e in s["entities"] if isinstance(e, dict) and isinstance(e.get("attributes"), dict) for k in _DECISION_ATTRIBUTES if e["attributes"].get(k)}
    problems += [f"publish gate: decision {d} is cited but not in decisions.jsonl" for d in sorted(cited - known)]

    checked = {
        "bundle_format": document["bundle_format"], "tier": tier, "repository_id": manifest.repository_id, "commit_sha": manifest.commit_sha, "rkp_hash": manifest.rkp_hash,
        "units": len(units), **totals, "files": actual["files"],
        "content_hash": "re-derived from the rows" if full else "not re-derivable: the architecture tier leaves detail out, so each unit is checked against its own digest",
    }
    return problems, checked


def _verify(args: argparse.Namespace) -> int:
    """`rkp verify`: exit 0 when the bundle is consistent and passes the publish gate, else 1."""
    try:
        source = _directory_source(args.path)
        try:
            problems, checked = _verify_bundle(_load_bundle(source))
        except bundle.BundleError as error:
            problems, checked = [str(error)], {}
    except Exception as error:  # reported, never a traceback on an editor's stdout
        message = str(error) or type(error).__name__
        if args.json:
            _emit("error", message=message, kind=type(error).__name__)
        else:
            print(f"rkp: {message}", file=sys.stderr)
        return 1
    if args.json:
        _emit("result", ok=not problems, bundle=source.label, problems=problems, checked=checked)
    elif problems:
        print(f"{len(problems)} problem{'s' if len(problems) != 1 else ''} in {source.knowledge}:")
        for problem in problems:
            print(f"  {problem}")
    else:
        print(f"clean: {checked['repository_id']} at {checked['commit_sha'][:12]}, {checked['tier']} tier")
        print(f"  {checked['units']} units, {checked['entities']} entities, {checked['assertions']} assertions, {checked['evidence']} evidence rows, {checked['files']} files")
        print(f"  every digest matches and the publish gate passes; content hash {checked['content_hash']}")
    return 1 if problems else 0


# ------------------------------------------------------------------ rkp diff


def _add_diff_command(commands: Any) -> None:
    diff = commands.add_parser("diff", help="structural difference between two bundles: two directories, or two commits of one repository")
    diff.add_argument("a", help="the older bundle: a directory containing .rkp/, or a commit, branch or tag")
    diff.add_argument("b", help="the newer bundle, named the same way")
    diff.add_argument("--repo", default=".", help="the repository whose commits A and B name, when they are not directories (default: .)")
    diff.add_argument("--json", action="store_true", help="one JSON object on stdout")


def _diff_source(name: str, repo: str) -> _DirectorySource | _GitSource:
    """A directory when `name` is one (it takes precedence over a ref of the same name), else a commit."""
    if Path(name).is_dir():
        return _directory_source(name)
    return _GitSource(bundle.repository_root(repo), name)


def _entities(loaded: dict[str, Any], leave_out: tuple[str, ...]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Entities by (kind, natural key), merged across units as the engine merges them; and id -> key.

    Keyed by kind and natural key rather than ID, so two repositories' bundles compare too
    (an entity ID folds in the repository ID).
    """
    out: dict[str, dict[str, Any]] = {}
    key_of: dict[str, str] = {}
    for shard in loaded["shards"].values():
        for e in shard["entities"]:
            if e["kind"] in leave_out:
                continue
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


def _coverage_changes(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, Any]:
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


def _diff_bundles(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Structural difference from bundle `a` (older) to `b` (newer).

    Entities are matched by kind and natural key; an entity whose key changed counts as moved
    when repo_intel.knowledge.delta.match_moves links the two (same kind and name, and shared
    content-derived evidence). Relations are compared as claims (subject, predicate, object,
    qualifiers), with moved entities mapped onto their new keys, as the snapshot delta does.
    When one bundle is architecture tier and the other full, both are compared at the
    architecture tier, so function-level detail does not show as added or removed.

    The snapshot delta itself (delta.knowledge_delta) is not used: it needs the toolchain and
    path manifests from the cache, which a bundle does not carry, it refuses two repositories,
    and it truncates its rows.
    """
    from repo_intel.knowledge.delta import COMPILE_ARTEFACTS, PLANE_OF, match_moves

    tier = "architecture" if "architecture" in (a["tier"], b["tier"]) else "full"
    leave_out = tuple(bundle.DETAIL_KINDS) if tier == "architecture" else ()
    ents_a, key_a = _entities(a, leave_out)
    ents_b, key_b = _entities(b, leave_out)
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
        compared = (set(before["attributes"]) | set(after["attributes"])) - COMPILE_ARTEFACTS - set(_DECISION_ATTRIBUTES)
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
        "coverage": _coverage_changes(a["tables"]["coverage"], b["tables"]["coverage"]),
        "counts": {k: {"before": counts_a.get(k), "after": counts_b.get(k)} for k in ("conflicts", "repository_unknowns", "decisions") if counts_a.get(k) != counts_b.get(k)},
        "files": {"added": sorted(files_b.keys() - files_a.keys()), "removed": sorted(files_a.keys() - files_b.keys()), "modified": sorted(p for p in files_a.keys() & files_b.keys() if files_a[p] != files_b[p])},
        "warnings": [f"{loaded['source'].label}: {p}" for loaded in (a, b) for p in loaded["problems"]],
    }


def _ratio_text(r: dict[str, int] | None) -> str:
    return "none" if r is None else f"{r['numerator']}/{r['denominator']}"


def _print_diff(result: dict[str, Any]) -> None:
    for label, side in (("a", result["from"]), ("b", result["to"])):
        print(f"{label}: {side['bundle']}: {side['repository_id']} at {side['commit_sha'][:12]}, {side['tier']} tier")
    if result["compared_tier"] == "architecture" and "full" in (result["from"]["tier"], result["to"]["tier"]):
        print("  compared at the architecture tier: function-level detail is left out of both")
    if not result["same_repository"]:
        print("  two repositories: entities are matched by kind and natural key")
    for warning in result["warnings"]:
        print(f"  warning: {warning}")
    entities, relations, coverage, files = result["entities"], result["relations"], result["coverage"], result["files"]
    s = entities["summary"]
    if not any(s[c] for c in ("added", "removed", "changed", "moved")) and not any(relations["summary"].values()) and not coverage["entries"] and not any(files.values()) and not result["counts"]:
        print("No structural differences." + (f" {s['evidence_only']} entities cite edited files without changing." if s["evidence_only"] else ""))
        return
    width = max([len(k) for k in [*entities["by_kind"], *relations["by_predicate"]]] + [10])
    print(f"entities: {s['added']} added, {s['removed']} removed, {s['changed']} changed, {s['moved']} moved; {s['evidence_only']} only cite edited files")
    for kind, n in entities["by_kind"].items():
        print(f"  {kind:<{width}}  +{n['added']}  -{n['removed']}  ~{n['changed']}" + (f"  moved {n['moved']}" if n["moved"] else ""))
    r = relations["summary"]
    print(f"relations: {r['added']} added, {r['removed']} removed, {r['support_changed']} with changed support")
    for predicate, n in relations["by_predicate"].items():
        print(f"  {predicate:<{width}}  +{n['added']}  -{n['removed']}" + (f"  support {n['support_changed']}" if n["support_changed"] else ""))
    if coverage["entries"]:
        print("coverage:")
        for row in coverage["entries"]:
            same = row["before"] == row["after"]
            print(f"  {row['domain']} {row['scope']}: {_ratio_text(row['before'])} -> {_ratio_text(row['after'])}{' (detail changed)' if same else ''}")
            for construct, change in row.get("constructs", {}).items():
                before, after = change["before"] or {"seen": 0, "modelled": 0}, change["after"] or {"seen": 0, "modelled": 0}
                print(f"    {construct}: modelled {before['modelled']} of {before['seen']} -> {after['modelled']} of {after['seen']}")
    for key, change in result["counts"].items():
        print(f"{key.replace('_', ' ')}: {change['before']} -> {change['after']}")
    print(f"files: {len(files['added'])} added, {len(files['removed'])} removed, {len(files['modified'])} modified")


def _diff(args: argparse.Namespace) -> int:
    """`rkp diff`: exit 0 with the difference (none included), 1 when a bundle cannot be read."""
    try:
        result = _diff_bundles(_load_bundle(_diff_source(args.a, args.repo)), _load_bundle(_diff_source(args.b, args.repo)))
    except Exception as error:  # reported, never a traceback on an editor's stdout
        message = str(error) or type(error).__name__
        if args.json:
            _emit("error", message=message, kind=type(error).__name__)
        else:
            print(f"rkp: {message}", file=sys.stderr)
        return 1
    if args.json:
        _emit("result", **result)
    else:
        _print_diff(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
