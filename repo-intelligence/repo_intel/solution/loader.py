"""Load a solution: each member's `.rkp/` bundle, and the solution's own inputs.

Nothing is compiled. A member compiled on this machine is read from its own cache, with
function-level detail and its repository exports (the HTTP calls no endpoint of its own
serves). Otherwise its committed bundle is hydrated, as the editor's local API does,
into the solution's cache (`.rkp/cache/solution/<member id>/`), so loading never writes
into a member's directory. A hydrated bundle is checked against its digests and refused
whole when it does not match; its HTTP calls are not available until the member is
compiled on this machine, and the loader says so.

The solution's inputs (`inputs` in the manifest, files under `.rkp/inputs/`) are read by
the platform packs whose `inputs` patterns match them, one file at a time, exactly as a
compile would read them, and pooled into a member named `inputs`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repo_intel.compile.contracts import Assertion, Entity, Evidence, Manifest
from repo_intel.compile.ontology import AssertionKind, EntityKind, EpistemicStatus, EvidenceClass, Predicate, check_relation
from repo_intel.solution.manifest import INPUTS_DIR, INPUTS_MEMBER, MANIFEST, Member, SolutionError, SolutionManifest, load_manifest
from repo_intel.solution.view import MemberData, SolutionView

CACHE = ".rkp/cache/solution"
CATALOG_ROLES = frozenset({"database"})


@dataclass
class Solution:
    root: Path
    manifest: SolutionManifest
    view: SolutionView
    problems: list[str] = field(default_factory=list)  # a member or input that could not be read
    warnings: list[str] = field(default_factory=list)  # read, with something missing
    input_files: list[str] = field(default_factory=list)


def _pooled(rows: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """One entity declared in several shards, pooled as the local API's entity detail does."""
    entity = rows[0][1]
    for _, other in rows[1:]:
        entity = {**entity, "attributes": {**other["attributes"], **entity["attributes"]}, "evidence_ids": sorted(set(entity["evidence_ids"]) | set(other["evidence_ids"]))}
    return entity


def _open_member(root: Path, member: Member, cache: Path) -> MemberData:
    """A member's bundle as rows. Raises BundleError or SolutionError when it cannot be read."""
    from repo_intel.knowledge import bundle
    from repo_intel.knowledge.local_api import Snapshot
    from repo_intel.storage.store import ObjectStore

    directory = member.directory(root)
    if not (directory / bundle.BUNDLE).is_dir():
        raise SolutionError(f"no .rkp/ directory at {member.source}")
    document = bundle.read_manifest(directory)
    if document is None:
        raise SolutionError(f"no bundle has been written at {member.source}; run `rkp compile` there")
    manifest = Manifest.model_validate(document["snapshot"])
    exports = None
    own = directory / bundle.BUNDLE / "cache" / bundle.TENANT
    store = bundle.store_for(directory) if own.is_dir() else None
    if store is not None and bundle.has_snapshot(store, manifest):  # compiled on this machine
        snapshot = Snapshot(store, manifest)
        ref = store.index_get("workspace_exports", manifest.rkp_hash)
        exports = store.get_json(ref) if ref and store.has(ref) else None
    else:
        target = ObjectStore(cache / member.id, bundle.TENANT)
        target, loaded = bundle.hydrate(directory, target)
        rank = {ref.unit: ref.digest for ref in manifest.shards}
        snapshot = Snapshot(target, loaded, rank, "architecture" if document.get("tier") == "architecture" else "complete")
    entities = {entity_id: _pooled(rows) for entity_id, rows in snapshot.entity_rows.items()}
    store = snapshot.store
    return MemberData(
        id=member.id, role=member.role, source=member.source, repository_id=manifest.repository_id,
        entities=entities, assertions=dict(snapshot.assertions), evidence=dict(snapshot.evidence),
        coverage=store.get_json(snapshot.manifest.coverage_ref), unknowns=store.get_json(snapshot.manifest.unknowns_ref),
        conflicts=store.get_json(snapshot.manifest.conflicts_ref), exports=exports, database=member.database,
        info={"rkp_hash": manifest.rkp_hash, "commit_sha": manifest.commit_sha, "status": manifest.status, "tier": document.get("tier", "full"), "detail": snapshot.detail, "working_tree": document.get("working_tree", False)},
        is_catalog=member.role in CATALOG_ROLES,
    )


# ------------------------------------------------------------------ the solution's own inputs


def input_files(root: Path, patterns: list[str]) -> tuple[list[str], list[str]]:
    """The files the manifest's `inputs` name, relative to `root`, and the problems found."""
    base = (root / INPUTS_DIR).resolve()
    found: set[str] = set()
    problems = []
    for pattern in patterns:
        hits = [p for p in sorted(root.glob(pattern)) if p.is_file()]
        if not hits:
            problems.append(f"input {pattern} matches no file")
        for p in hits:
            if p.is_symlink() or not p.resolve().is_relative_to(base):
                problems.append(f"input {p.relative_to(root).as_posix()} is not a file under {INPUTS_DIR}")
                continue
            found.add(p.relative_to(root).as_posix())
    return sorted(found), problems


def _readers(path: str) -> list[Any]:
    """The extractors of every platform pack whose `inputs` patterns match `path`."""
    from repo_intel import platforms

    out = []
    for pack in platforms.packs().values():
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in pack.inputs):
            out += [e for e in pack.extractors if e.matches(path)]
    return out


def _status(evidence_class: str) -> EpistemicStatus:
    return EpistemicStatus.SUPPORTED if evidence_class in ("E1", "E2", "E3") else EpistemicStatus.INFERRED


def read_inputs(root: Path, manifest: SolutionManifest) -> tuple[MemberData, list[str], list[str], list[str]]:
    """The solution's inputs as a member: (member, files read, problems, warnings).

    Each file is read by its packs' extractors as a compile reads it: secret values masked
    first, files over the parse limit inventoried only. The observations are pooled the way
    a unit's are compiled: entities merged by identity, a relation kept only when both its
    ends are declared or it may declare them.
    """
    from repo_intel import platforms
    from repo_intel.compile.compiler import platform_construct_coverage
    from repo_intel.ingest import secrets
    from repo_intel.ingest.snapshot import MAX_PARSE_BYTES

    repository_id = f"solution/{manifest.name}"
    files, problems = input_files(root, manifest.inputs)
    warnings: list[str] = []
    entities: dict[str, dict[str, Any]] = {}
    assertions: dict[str, dict[str, Any]] = {}
    evidence: dict[str, dict[str, Any]] = {}
    relations: list[tuple[str, dict[str, Any], str]] = []  # (path, observation row, artifact id)

    def cite(row: dict[str, Any]) -> str:
        evidence[row["id"]] = Evidence.model_validate(row).row()
        return row["id"]

    def declare(e: Entity) -> str:
        row = e.row()
        old = entities.get(e.id)
        if old:
            row["attributes"] = {**old["attributes"], **row["attributes"]}
            row["evidence_ids"] = sorted(set(old["evidence_ids"]) | set(row["evidence_ids"]))
        entities[e.id] = row
        return e.id

    def assert_(a: Assertion) -> None:
        row = a.row()
        old = assertions.get(a.id)
        if old:
            row["evidence_ids"] = sorted(set(old["evidence_ids"]) | set(row["evidence_ids"]))
        assertions[a.id] = row

    for path in files:
        data = (root / path).read_bytes()
        blob = hashlib.sha256(data).hexdigest()
        readers = _readers(path)
        attributes: dict[str, Any] = {"language": None, "parsed": False}
        observations = []
        if not readers:
            warnings.append(f"input {path}: no platform pack reads this file")
        elif len(data) > MAX_PARSE_BYTES:
            warnings.append(f"input {path}: over the parse limit of {MAX_PARSE_BYTES} bytes, not read")
        else:
            findings = secrets.scan(data)
            if findings:
                warnings.append(f"input {path}: {len(findings)} secret values were masked before reading")
            masked = secrets.mask_for_extraction(data, findings)
            for reader in readers:
                try:
                    result = reader.extract(path, masked, blob)
                except Exception as error:  # a reader's bug is reported, never fatal
                    problems.append(f"input {path}: {reader.id} failed: {type(error).__name__}: {error}")
                    continue
                attributes["parsed"] = attributes["parsed"] or result.parsed
                observations += [o.row() for o in result.observations]
                for d in result.diagnostics:
                    if d.severity != "info":
                        warnings.append(f"input {path}: {d.message}")
        constructs: dict[str, dict[str, int]] = {}
        for o in observations:
            if o["kind"] == "construct":
                count = constructs.setdefault(o["payload"]["construct"], {"seen": 0, "modelled": 0})
                count["seen"] += 1
                count["modelled"] += 1 if o["payload"]["modelled"] else 0
        if constructs:
            attributes["constructs"] = dict(sorted(constructs.items()))
        file_ev = Evidence.make(blob, _whole(data), "file").row()
        artifact = declare(Entity.make(repository_id, EntityKind.ARTIFACT, {"path": path}, posixpath.basename(path), attributes=attributes, evidence_ids=[cite(file_ev)]))
        for o in observations:
            p = o["payload"]
            if o["kind"] == "entity":
                ev = cite(o["evidence"])
                eid = declare(Entity.make(repository_id, EntityKind(p["kind"]), p["natural_key"], p["name"], attributes=p.get("attributes", {}), evidence_ids=[ev]))
                assert_(Assertion.make(eid, Predicate.DEFINED_IN, artifact, assertion_kind=AssertionKind.DERIVED_RELATION, epistemic_status=_status(o["evidence_class"]), evidence_class=o["evidence_class"], evidence_ids=[ev]))
            elif o["kind"] == "relation":
                relations.append((path, o, artifact))

    def resolve(end: Any, artifact: str, ev: str) -> str | None:
        if end == "@file":
            return artifact
        if not isinstance(end, dict):
            return None  # "@package": the inputs belong to no package
        eid = Entity.make(repository_id, EntityKind(end["kind"]), end["natural_key"], end["name"]).id
        if eid not in entities:
            if end.get("declare", True) is False:
                return None
            declare(Entity.make(repository_id, EntityKind(end["kind"]), end["natural_key"], end["name"], evidence_ids=[ev]))
        return eid

    def declaring(o: dict[str, Any]) -> bool:
        return all(not isinstance(o["payload"][end], dict) or o["payload"][end].get("declare", True) for end in ("subject", "object"))

    for path, o, artifact in [r for r in relations if declaring(r[1])] + [r for r in relations if not declaring(r[1])]:
        p = o["payload"]
        ev = o["evidence"]["id"]
        subject, obj = resolve(p["subject"], artifact, ev), resolve(p["object"], artifact, ev)
        if subject is None or obj is None:
            continue
        cite(o["evidence"])
        assert_(Assertion.make(subject, Predicate(p["predicate"]), obj, qualifiers=p.get("qualifiers", {}), assertion_kind=AssertionKind.OBSERVED_FACT, epistemic_status=_status(o["evidence_class"]), evidence_class=o["evidence_class"], evidence_ids=[ev]))

    # The checks the publish gate applies to a bundle: relation schema and platform terms.
    for a in assertions.values():
        reason = check_relation(Predicate(a["predicate"]), EntityKind(entities[a["subject"]]["kind"]), EntityKind(entities[a["object"]]["kind"]), a["evidence_class"])
        if reason:
            problems.append(f"inputs: {reason}")
    for e in entities.values():
        reason = platforms.check_platform_kind(e["kind"], e["attributes"])
        if reason:
            problems.append(f"inputs: {e['kind']} {e['name']}: {reason}")
    coverage = platform_construct_coverage({INPUTS_MEMBER: {"entities": list(entities.values())}})
    member = MemberData(
        id=INPUTS_MEMBER, role="inputs", source=INPUTS_DIR.rstrip("/"), repository_id=repository_id,
        entities=dict(sorted(entities.items())), assertions=dict(sorted(assertions.items())), evidence=dict(sorted(evidence.items())),
        coverage=coverage, exports={"operations": [], "unresolved_calls": []}, info={"files": len(files)}, is_catalog=True,
    )
    return member, files, problems, warnings


def _whole(data: bytes):
    from repo_intel.compile.contracts import Span

    return Span(start_byte=0, end_byte=len(data), start_line=1, end_line=data.count(b"\n", 0, max(0, len(data) - 1)) + 1)


def load_solution(root: str | Path, cache: str | Path | None = None) -> Solution:
    """Read `.rkp/solution.json` under `root` and load every member and input.

    A member that cannot be read is left out of the view and named in `problems`; the
    rest load. Raises SolutionError only when the manifest itself is missing or invalid.
    """
    from repo_intel.knowledge.bundle import BundleError

    root = Path(root)
    manifest = load_manifest(root)
    cache_dir = Path(cache) if cache is not None else root / CACHE
    members: list[MemberData] = []
    problems: list[str] = []
    warnings: list[str] = []
    for member in manifest.members:
        try:
            data = _open_member(root, member, cache_dir)
        except (BundleError, SolutionError, OSError, ValueError) as error:
            problems.append(f"member {member.id}: {error}")
            continue
        if data.exports is None:
            warnings.append(f"member {member.id}: its HTTP calls are not in the committed bundle; compile it on this machine (`rkp compile {member.source}`) to link them")
        members.append(data)
    files: list[str] = []
    if manifest.inputs:
        inputs, files, input_problems, input_warnings = read_inputs(root, manifest)
        members.append(inputs)
        problems += input_problems
        warnings += input_warnings
    return Solution(root=root, manifest=manifest, view=SolutionView(members), problems=problems, warnings=warnings, input_files=files)


__all__ = ["CACHE", "MANIFEST", "Solution", "input_files", "load_solution", "read_inputs"]
