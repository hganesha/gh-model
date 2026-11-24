"""Incremental/cold parity (spec §6 safety net, §14.2; GI-019).

An incremental compile and a cold one evaluate the same build graph, so they
must agree on `content_hash`. A mismatch means a step is impure or reads an
undeclared input. `compare` names exactly which shards (and which other
content-hash leaves) differ, so an incident points at the unit to look at.

    python -m repo_intel.knowledge.parity [fixture ...]

compiles every seeded commit of each fixture twice, once in a warm store in
sequence and once in a fresh store per commit, and exits 1 on any mismatch.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repo_intel.compile.contracts import Manifest
from repo_intel.storage.hashing import digest
from repo_intel.storage.store import ObjectStore

LEAVES = ("coverage_ref", "conflicts_ref")


@dataclass
class ParityReport:
    incremental: str
    cold: str
    match: bool
    differing_shards: list[dict[str, Any]] = field(default_factory=list)
    differing_refs: list[str] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        return {"incremental": self.incremental, "cold": self.cold, "match": self.match,
                "differing_shards": self.differing_shards, "differing_refs": self.differing_refs}


def compare(incremental: Manifest, cold: Manifest, inc_store: ObjectStore | None = None, cold_store: ObjectStore | None = None) -> ParityReport:
    """Name the shards and content leaves on which two compiles of one commit disagree.

    With stores given, each differing shard also lists the entity and assertion
    IDs present on only one side, and those whose rows differ.
    """
    a, b = {s.unit: s.digest for s in incremental.shards}, {s.unit: s.digest for s in cold.shards}
    shards = []
    for unit in sorted(a.keys() | b.keys()):
        if a.get(unit) == b.get(unit):
            continue
        row: dict[str, Any] = {"unit": unit, "incremental": a.get(unit), "cold": b.get(unit)}
        if inc_store is not None and cold_store is not None and unit in a and unit in b:
            row["rows"] = _row_diff(inc_store.get_json(a[unit]), cold_store.get_json(b[unit]))
        shards.append(row)
    refs = [leaf for leaf in LEAVES if getattr(incremental, leaf) != getattr(cold, leaf)]
    match = incremental.content_hash == cold.content_hash
    if match != (not shards and not refs):
        refs.append("content_hash")  # the leaves agree but the root does not: report it rather than hide it
    return ParityReport(incremental.rkp_hash, cold.rkp_hash, match and not shards and not refs, shards, refs)


def _row_diff(x: dict[str, Any], y: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for table in ("entities", "assertions", "evidence", "unknowns"):
        left = {r["id"]: digest(r) for r in x.get(table, [])}
        right = {r["id"]: digest(r) for r in y.get(table, [])}
        diff = {
            "only_incremental": sorted(left.keys() - right.keys())[:50],
            "only_cold": sorted(right.keys() - left.keys())[:50],
            "changed": sorted(k for k in left.keys() & right.keys() if left[k] != right[k])[:50],
        }
        if any(diff.values()):
            out[table] = diff
    return out


def cold_compile(store: ObjectStore, repository_id: str, repo_path: str | os.PathLike[str], commit_sha: str, **kwargs: Any):
    """Compile with the step cache bypassed. Every step executes; decision
    ledger entries are still reused (frozen ledger), as replay requires."""
    from repo_intel.compile.pipeline import stage_compile, stage_snapshot
    return stage_compile(store, repository_id, stage_snapshot(store, repo_path, commit_sha), use_cache=False, **kwargs)


def sampled(rkp_hash: str, rate: float) -> bool:
    """Deterministic sample of compiles for shadow checking: the same snapshot always decides the same way."""
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    return int(rkp_hash.split(":", 1)[-1][:8], 16) / 0xFFFFFFFF < rate


def check_fixture(name: str, root: Path, repository_id: str = "github.com/1001") -> list[ParityReport]:
    """Compile a fixture's seeded commits warm (in sequence) and cold (fresh store each); report each commit."""
    from repo_intel.compile.fixtures import materialize
    from repo_intel.compile.pipeline import compile_commit

    shas = materialize(name, root / "repo")
    warm = ObjectStore(root / "warm", "t")
    reports = []
    for i, sha in enumerate(shas):
        cold_store = ObjectStore(root / f"cold{i}", "t")
        inc = compile_commit(warm, root / "repo", repository_id, sha).manifest
        cold = compile_commit(cold_store, root / "repo", repository_id, sha).manifest
        reports.append(compare(inc, cold, warm, cold_store))
    return reports


def main(argv: list[str] | None = None) -> int:
    from repo_intel.compile.fixtures import list_fixtures

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("fixtures", nargs="*")
    args = ap.parse_args(argv)
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in args.fixtures or list_fixtures():
            reports = check_fixture(name, Path(tmp) / name)
            bad = [r for r in reports if not r.match]
            print(f"{name}: {len(reports)} commit(s), {'parity' if not bad else f'{len(bad)} mismatch(es)'}")
            for r in bad:
                print("   -", r.incremental, "vs", r.cold, "differing shards:", [s["unit"] for s in r.differing_shards], "refs:", r.differing_refs)
            failed += bool(bad)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
