"""What `rkp solution check` and `rkp solution links` report, as data and as text.

Reports name members by id and paths as the manifest gives them, never as absolute
paths, so a report can be shared.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from repo_intel.solution.identity import resolve
from repo_intel.solution.loader import load_solution
from repo_intel.solution.manifest import INPUTS_MEMBER


def check(root: str | Path, cache: str | Path | None = None) -> dict[str, Any]:
    """Validate the manifest, open every member's bundle and read the inputs. `ok` is false
    when a member or input could not be read, or the inputs break the relation schema."""
    solution = load_solution(root, cache)
    view = solution.view
    counts = view.counts()
    members = []
    for m in solution.manifest.members:
        data = view.members.get(m.id)
        row: dict[str, Any] = {"id": m.id, "role": m.role, "kind": m.kind, "source": m.source, "loaded": data is not None}
        if m.database:
            row["database"] = m.database
        if data is not None:
            row.update({
                "repository_id": data.repository_id, **{k: data.info[k] for k in ("commit_sha", "rkp_hash", "status", "tier", "detail") if k in data.info},
                "entities": len(data.entities), "assertions": len(data.assertions), "kinds": counts[m.id],
                "calls_available": data.exports is not None,
            })
        members.append(row)
    inputs = view.members.get(INPUTS_MEMBER)
    return {
        "solution": solution.manifest.name, "schema": solution.manifest.schema_, "ok": not solution.problems,
        "problems": solution.problems, "warnings": solution.warnings, "members": members,
        "inputs": {"files": solution.input_files, "entities": len(inputs.entities) if inputs else 0, "kinds": counts.get(INPUTS_MEMBER, {}), "coverage": inputs.coverage if inputs else []},
    }


def links(root: str | Path, cache: str | Path | None = None) -> dict[str, Any]:
    """Identity links, candidates and unmatched references across the solution's members."""
    solution = load_solution(root, cache)
    resolution = resolve(solution.view)
    return {"solution": solution.manifest.name, "problems": solution.problems, "warnings": solution.warnings, **resolution.row()}


def _end(row: dict[str, Any], side: str) -> str:
    return f"{row[side + '_member']}:{row[side + '_kind']} {row[side + '_name']}"


def format_check(report: dict[str, Any]) -> list[str]:
    lines = [f"{'ok' if report['ok'] else 'problems'}: solution {report['solution']}, {len(report['members'])} members"]
    for m in report["members"]:
        if not m["loaded"]:
            lines.append(f"  {m['id']} ({m['role']}) {m['source']}: not loaded")
            continue
        calls = "" if m["calls_available"] else ", HTTP calls not available"
        lines.append(f"  {m['id']} ({m['role']}) {m['source']}: {m['repository_id']} at {m.get('commit_sha', '')[:12]}, {m['entities']} entities, {m.get('tier', 'full')} tier{calls}")
    if report["inputs"]["files"]:
        lines.append(f"  inputs: {len(report['inputs']['files'])} files, {report['inputs']['entities']} entities")
        for c in report["inputs"]["coverage"]:
            lines.append(f"    {c['domain']} {c['scope']}: {c['numerator']}/{c['denominator']} constructs modelled")
    lines += [f"  problem: {p}" for p in report["problems"]]
    lines += [f"  warning: {w}" for w in report["warnings"]]
    return lines


def format_links(report: dict[str, Any]) -> list[str]:
    lines = [f"solution {report['solution']}: {len(report['links'])} links, {len(report['candidates'])} candidates, {len(report['unmatched'])} unmatched"]
    if report["links"]:
        lines.append("links (E3, exact):")
        lines += [f"  {r['rule']}: {_end(r, 'from')} -{r['predicate']}-> {_end(r, 'to')}" for r in report["links"]]
    if report["candidates"]:
        lines.append("candidates (E4, never merged):")
        lines += [f"  {r['rule']}: {_end(r, 'from')} -{r['predicate']}-> {_end(r, 'to')}" for r in report["candidates"]]
    if report["unmatched"]:
        lines.append("unmatched references:")
        lines += [f"  {u['member']}: {u['kind']} {(u['method'] + ' ') if u.get('method') else ''}{u['reference']} ({u['reason']})" for u in report["unmatched"]]
    for c in report["coverage"]:
        lines.append(f"coverage {c['domain']} {c['scope']}: {c['numerator']}/{c['denominator']}")
    lines += [f"problem: {p}" for p in report["problems"]]
    lines += [f"warning: {w}" for w in report["warnings"]]
    return lines
