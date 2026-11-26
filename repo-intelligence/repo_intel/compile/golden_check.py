"""Compare what the compiler produces with fixture goldens.

    python -m repo_intel.compile.golden_check [--milestone M1] [--kinds Endpoint,Table] [fixture ...]

Prints every golden item due by the milestone that is missing, and every
absent or forbidden item that is present. Exit status 1 if anything is wrong.

A fixture whose `base/` holds `.rkp/solution.json` is multi-repository: `base/` is the
solution root, laid out as an owner would lay it out, with one directory per member.
Each commit's member directories are copied into repositories of their own and compiled
one by one, the solution is loaded from those bundles, and identity is resolved across
them. Its golden may name a member on any entry and lists identity links, unmatched
references and the solution's coverage (benchmarks/fixtures/README.md has the format).
Single-repository fixtures are read exactly as before.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from repo_intel.compile.contracts import entity_id
from repo_intel.compile.fixtures import FIXTURES_DIR, list_fixtures, load_golden, materialize
from repo_intel.compile.pipeline import compile_commit, load_shards
from repo_intel.storage.store import ObjectStore

REPO_ID = "github.com/1001"
ORDER = ["M0", "M1", "M2", "M3", "M4", "M5"]
COMPILER_MILESTONE = "M2"
SOLUTION_MANIFEST = Path(".rkp") / "solution.json"


def due(item: dict[str, Any], milestone: str) -> bool:
    return ORDER.index(item["since"]) <= ORDER.index(milestone)


def eid(item: dict[str, Any]) -> str:
    return entity_id(REPO_ID, item["kind"], item["natural_key"])


def is_solution(name: str) -> bool:
    """A multi-repository fixture: its base is a solution root."""
    return (FIXTURES_DIR / name / "base" / SOLUTION_MANIFEST).is_file()


def compile_fixture(name: str, root: Path) -> list[dict[str, Any]]:
    if is_solution(name):
        return compile_solution(name, root)
    shas = materialize(name, root / "repo")
    store = ObjectStore(root / "store", "t")
    out = []
    for sha in shas:
        result = compile_commit(store, root / "repo", REPO_ID, sha)
        shards = load_shards(store, result.manifest)
        out.append({"entities": {e["id"]: e for s in shards.values() for e in s["entities"]}, "assertions": [a for s in shards.values() for a in s["assertions"]], "manifest": result.manifest})
    return out


def check(name: str, commits: list[dict[str, Any]], milestone: str, kinds: set[str] | None = None) -> list[str]:
    if is_solution(name):
        if not commits or "members" not in commits[0]:
            # The caller compiled the fixture's tree as one repository; a multi-repository
            # golden is about its members, so compile those. Callers that already have
            # `compile_solution`'s output (tests/test_fixtures.py) pass it to avoid this.
            with tempfile.TemporaryDirectory() as tmp:
                commits = compile_solution(name, Path(tmp))
        return check_solution(load_golden(name), commits, milestone, kinds)
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


# ------------------------------------------------------------------ multi-repository fixtures

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "PATH": os.environ.get("PATH", ""),
}
# What a compile writes into a member's `.rkp/`; never part of the member's commits.
_MEMBER_OUTPUT = ("knowledge", "knowledge.new", "knowledge.old", "cache")


def _git(repo: Path, *args: str, date: str | None = None) -> str:
    env = dict(_GIT_ENV)
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    cmd = ["git", "-C", str(repo), "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false", *args]
    return subprocess.run(cmd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _sync_member(source: Path, dest: Path, index: int) -> None:
    """Make `dest`, a member's own repository, hold `source`'s files, and commit them."""
    if not (dest / ".git").is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        _git(dest, "init", "-q", "-b", "main")
        (dest / ".git" / "info").mkdir(exist_ok=True)
        (dest / ".git" / "info" / "exclude").write_text("".join(f"/.rkp/{d}/\n" for d in _MEMBER_OUTPUT))
    for child in dest.iterdir():
        if child.name == ".git":
            continue
        if child.name == ".rkp" and child.is_dir() and not child.is_symlink():
            for item in child.iterdir():
                if item.name not in _MEMBER_OUTPUT:
                    _remove(item)
            continue
        _remove(child)
    if source.is_dir():
        shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "--allow-empty", "-m", f"fixture commit {index}", date=f"2026-01-01T00:{index:02d}:00+00:00")


def compile_solution(name: str, root: Path) -> list[dict[str, Any]]:
    """Compile a multi-repository fixture, commit by commit.

    The fixture's layers are materialized as one Git repository (`root/tree`), as for any
    fixture. At each commit the solution root (`root/solution`) gets that commit's
    `.rkp/solution.json` and `.rkp/inputs/`, and each repository member's directory is
    copied into a repository of its own under it, committed and compiled into its bundle.
    The solution is then loaded from those bundles and identity resolved across them.

    Returns one entry per commit: `members` (member id -> repository_id, entities by
    local id, assertions), the resolution (`links`, `candidates`, `unmatched`,
    `coverage`), and the loader's `problems` and `warnings`.
    """
    from repo_intel.knowledge import bundle
    from repo_intel.solution import load_manifest, load_solution, resolve

    tree, solution_root = root / "tree", root / "solution"
    shas = materialize(name, tree)
    out = []
    for index, sha in enumerate(shas):
        _git(tree, "checkout", "-q", "--detach", sha)
        manifest = load_manifest(tree)
        rkp = solution_root / ".rkp"
        rkp.mkdir(parents=True, exist_ok=True)
        for item in ("solution.json", "inputs"):
            if (rkp / item).exists():
                _remove(rkp / item)
        shutil.copyfile(tree / SOLUTION_MANIFEST, rkp / "solution.json")
        if (tree / ".rkp" / "inputs").is_dir():
            shutil.copytree(tree / ".rkp" / "inputs", rkp / "inputs", symlinks=True)
        problems: list[str] = []
        for member in manifest.members:
            if member.kind != "repository":
                problems.append(f"member {member.id}: a fixture member must be a repository (`path`), not a delivered bundle")
                continue
            dest = member.directory(solution_root)
            _sync_member(member.directory(tree), dest, index)
            bundle.compile_bundle(dest)
        solution = load_solution(solution_root)
        resolution = resolve(solution.view).row()
        out.append({
            "sha": sha,
            "members": {
                m.id: {"repository_id": m.repository_id, "entities": dict(m.entities), "assertions": list(m.assertions.values()), "rkp_hash": m.info.get("rkp_hash")}
                for m in solution.view.members.values()
            },
            "resolution": resolution,
            "problems": problems + solution.problems,
            "warnings": solution.warnings,
        })
    return out


def _end_label(end: dict[str, Any]) -> str:
    return f"{end.get('member', '*')}:{end['kind']} {json.dumps(end['natural_key'], sort_keys=True)}"


def _ids(commit: dict[str, Any], end: dict[str, Any]) -> list[tuple[str, str]]:
    """(member, local entity id) for every member the entry may be in where the entity exists."""
    members = commit["members"]
    names = [end["member"]] if "member" in end else sorted(members)
    out = []
    for m in names:
        data = members.get(m)
        if data is None:
            continue
        local = entity_id(data["repository_id"], end["kind"], end["natural_key"])
        if local in data["entities"]:
            out.append((m, local))
    return out


def _namespaced(commit: dict[str, Any], end: dict[str, Any]) -> set[str]:
    return {f"{m}:{local}" for m, local in _ids(commit, end)}


def _has_link(commit: dict[str, Any], link: dict[str, Any]) -> bool:
    sources, targets = _namespaced(commit, link["from"]), _namespaced(commit, link["to"])
    records = commit["resolution"]["links"] + commit["resolution"]["candidates"]
    return any(r["rule"] == link["rule"] and r["from"] in sources and r["to"] in targets for r in records)


def _has_unmatched(commit: dict[str, Any], item: dict[str, Any]) -> bool:
    fields = ("member", "kind", "reference", "reason")
    return any(all(u.get(f) == item[f] for f in fields) for u in commit["resolution"]["unmatched"])


def _coverage_problems(commit: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    """Each wanted coverage entry must match exactly one resolution entry for its domain and scope."""
    problems = []
    for item in items:
        got = [(c["numerator"], c["denominator"]) for c in commit["resolution"]["coverage"] if c["domain"] == item["domain"] and c["scope"] == item["scope"]]
        if got != [(item["numerator"], item["denominator"])]:
            problems.append(f"coverage {item['domain']} {item['scope']}: {got[0] if got else 'absent'}, want {(item['numerator'], item['denominator'])}")
    return problems


def _link_label(link: dict[str, Any]) -> str:
    return f"{link['rule']} {_end_label(link['from'])} -> {_end_label(link['to'])}"


def _unmatched_label(item: dict[str, Any]) -> str:
    return f"{item['member']}: {item['kind']} {item['reference']} ({item['reason']})"


def check_solution(g: dict[str, Any], commits: list[dict[str, Any]], milestone: str, kinds: set[str] | None = None) -> list[str]:
    """Problems with a multi-repository golden `g` against `compile_solution`'s output.

    An entity, edge or link end that names a member must be in that member; one that does
    not may be in any member. Links match on rule and both ends in either the exact links
    or the candidates (a rule's name says which it is). `solution_coverage` at the top level
    is checked against the base, and a commit entry's own `solution_coverage` against that
    commit. With `kinds`, only entities, edges and links touching those kinds are checked,
    and unmatched references and coverage not at all.
    """
    on = lambda item: due(item, milestone)
    kind_ok = lambda *ks: kinds is None or any(k in kinds for k in ks)
    entity_wanted = lambda item: on(item) and kind_ok(item["kind"])
    link_wanted = lambda item: on(item) and kind_ok(item["link"]["from"]["kind"], item["link"]["to"]["kind"])
    other_wanted = lambda item: on(item) and kinds is None
    problems: list[str] = []
    for i, c in enumerate(commits):
        problems += [f"commit {i}: solution: {p}" for p in c["problems"]]
    base = commits[0]
    for item in filter(entity_wanted, g["entities"]):
        found = _ids(base, item)
        if not found:
            problems.append(f"missing entity {_end_label(item)}")
            continue
        for k, v in item.get("attributes", {}).items():
            values = [base["members"][m]["entities"][local]["attributes"].get(k) for m, local in found]
            if v not in values:
                problems.append(f"entity {_end_label(item)}: attribute {k}={values[0]!r}, want {v!r}")
    for e in g["edges"]:
        if not (on(e) and kind_ok(e["subject"]["kind"], e["object"]["kind"])):
            continue
        member = e.get("member")
        subject, obj = ({**end, "member": member} if member else end for end in (e["subject"], e["object"]))
        pairs = {(m, s, o) for m, s in _ids(base, subject) for m2, o in _ids(base, obj) if m == m2}
        if not any(a["subject"] == s and a["object"] == o and a["predicate"] == e["predicate"] for m, s, o in pairs for a in base["members"][m]["assertions"]):
            problems.append(f"missing edge {_end_label(subject)} -{e['predicate']}-> {_end_label(obj)}")
    for item in g["absent_entities"]:
        if _ids(base, item):
            problems.append(f"present but must be absent: {_end_label(item)} ({item['why']})")
    for item in filter(link_wanted, g.get("links", [])):
        if not _has_link(base, item["link"]):
            problems.append(f"missing link {_link_label(item['link'])}")
    for item in g.get("absent_links", []):
        if _has_link(base, item["link"]):
            problems.append(f"link present but must be absent: {_link_label(item['link'])} ({item['why']})")
    for item in filter(other_wanted, g.get("unmatched", [])):
        if not _has_unmatched(base, item):
            problems.append(f"missing unmatched reference {_unmatched_label(item)}")
    problems += _coverage_problems(base, list(filter(other_wanted, g.get("solution_coverage", []))))
    forbidden = set(g["forbidden_kinds"])
    for i, c in enumerate(commits):
        bad = sorted({e["kind"] for m in c["members"].values() for e in m["entities"].values()} & forbidden)
        if bad:
            problems.append(f"commit {i}: forbidden kinds present: {bad}")
    for i, spec in enumerate(g["commits"], start=1):
        before, after = commits[i - 1], commits[i]
        for item in filter(entity_wanted, spec["added_entities"]):
            if not _ids(after, item) or _ids(before, item):
                problems.append(f"{spec['dir']}: should add {_end_label(item)}")
        for item in filter(entity_wanted, spec["removed_entities"]):
            if not _ids(before, item) or _ids(after, item):
                problems.append(f"{spec['dir']}: should remove {_end_label(item)}")
        for item in filter(link_wanted, spec.get("added_links", [])):
            if not _has_link(after, item["link"]) or _has_link(before, item["link"]):
                problems.append(f"{spec['dir']}: should add link {_link_label(item['link'])}")
        for item in filter(link_wanted, spec.get("removed_links", [])):
            if not _has_link(before, item["link"]) or _has_link(after, item["link"]):
                problems.append(f"{spec['dir']}: should remove link {_link_label(item['link'])}")
        for item in filter(other_wanted, spec.get("added_unmatched", [])):
            if not _has_unmatched(after, item) or _has_unmatched(before, item):
                problems.append(f"{spec['dir']}: should add unmatched reference {_unmatched_label(item)}")
        for item in filter(other_wanted, spec.get("removed_unmatched", [])):
            if not _has_unmatched(before, item) or _has_unmatched(after, item):
                problems.append(f"{spec['dir']}: should remove unmatched reference {_unmatched_label(item)}")
        problems += [f"{spec['dir']}: {p}" for p in _coverage_problems(after, list(filter(other_wanted, spec.get("solution_coverage", []))))]
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
