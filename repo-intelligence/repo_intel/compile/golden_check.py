"""Compare what the compiler produces with fixture goldens.

    python -m repo_intel.compile.golden_check [--milestone M1] [--kinds Endpoint,Table] [fixture ...]

Prints every golden item due by the milestone that is missing, and every
absent or forbidden item that is present. Exit status 1 if anything is wrong.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any

from repo_intel.compile.contracts import entity_id
from repo_intel.compile.fixtures import list_fixtures, load_golden, materialize
from repo_intel.compile.pipeline import compile_commit, load_shards
from repo_intel.storage.store import ObjectStore

REPO_ID = "github.com/1001"
ORDER = ["M0", "M1", "M2", "M3", "M4", "M5"]
COMPILER_MILESTONE = "M2"


def due(item: dict[str, Any], milestone: str) -> bool:
    return ORDER.index(item["since"]) <= ORDER.index(milestone)


def eid(item: dict[str, Any]) -> str:
    return entity_id(REPO_ID, item["kind"], item["natural_key"])


def compile_fixture(name: str, root: Path) -> list[dict[str, Any]]:
    shas = materialize(name, root / "repo")
    store = ObjectStore(root / "store", "t")
    out = []
    for sha in shas:
        result = compile_commit(store, root / "repo", REPO_ID, sha)
        shards = load_shards(store, result.manifest)
        out.append({"entities": {e["id"]: e for s in shards.values() for e in s["entities"]}, "assertions": [a for s in shards.values() for a in s["assertions"]], "manifest": result.manifest})
    return out


def check(name: str, commits: list[dict[str, Any]], milestone: str, kinds: set[str] | None = None) -> list[str]:
    g = load_golden(name)
    want = lambda item: due(item, milestone) and (kinds is None or item["kind"] in kinds)
    problems: list[str] = []
    base = commits[0]
    for item in filter(want, g["entities"]):
        got = base["entities"].get(eid(item))
        if got is None:
            problems.append(f"missing entity {item['kind']} {item['natural_key']}")
        else:
            for k, v in item.get("attributes", {}).items():
                if got["attributes"].get(k) != v:
                    problems.append(f"entity {item['kind']} {item['natural_key']}: attribute {k}={got['attributes'].get(k)!r}, want {v!r}")
    for e in g["edges"]:
        if due(e, milestone) and (kinds is None or e["subject"]["kind"] in kinds or e["object"]["kind"] in kinds):
            s, o = eid(e["subject"]), eid(e["object"])
            if not any(a["subject"] == s and a["object"] == o and a["predicate"] == e["predicate"] for a in base["assertions"]):
                problems.append(f"missing edge {e['subject']['kind']} {e['subject']['natural_key']} -{e['predicate']}-> {e['object']['kind']} {e['object']['natural_key']}")
    for item in g["absent_entities"]:
        if eid(item) in base["entities"]:
            problems.append(f"present but must be absent: {item['kind']} {item['natural_key']} ({item['why']})")
    forbidden = set(g["forbidden_kinds"])
    for i, c in enumerate(commits):
        bad = sorted({e["kind"] for e in c["entities"].values()} & forbidden)
        if bad:
            problems.append(f"commit {i}: forbidden kinds present: {bad}")
    for i, spec in enumerate(g["commits"], start=1):
        before, after = commits[i - 1]["entities"], commits[i]["entities"]
        for item in filter(want, spec["added_entities"]):
            if eid(item) not in after or eid(item) in before:
                problems.append(f"{spec['dir']}: should add {item['kind']} {item['natural_key']}")
        for item in filter(want, spec["removed_entities"]):
            if eid(item) not in before or eid(item) in after:
                problems.append(f"{spec['dir']}: should remove {item['kind']} {item['natural_key']}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixtures", nargs="*")
    ap.add_argument("--milestone", default=COMPILER_MILESTONE)
    ap.add_argument("--kinds", default=None, help="comma-separated entity kinds to restrict the check to")
    args = ap.parse_args()
    kinds = set(args.kinds.split(",")) if args.kinds else None
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in args.fixtures or list_fixtures():
            problems = check(name, compile_fixture(name, Path(tmp) / name), args.milestone, kinds)
            print(f"{name}: {'ok' if not problems else f'{len(problems)} problem(s)'}")
            for p in problems:
                print("   -", p)
            failed += bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
