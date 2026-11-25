"""Command-line engine: compile a repository into its `.rkp/` knowledge bundle.

    rkp compile [PATH] [--ref REF] [--working-tree] [--json]
    rkp status  [PATH] [--ref REF] [--working-tree] [--json]

    rkp serve   [PATH] --stdio

    rkp verify  [PATH] [--json]
    rkp diff    A B [--repo PATH] [--json]

`verify` re-checks the committed `.rkp/knowledge/` without compiling; `diff` compares two
bundles, read from two directories or from two commits of one repository
(repo_intel.knowledge.bundle_check). `serve --stdio` answers the Explorer's read API from the bundle, one JSON request per
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
import sys
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


# ------------------------------------------------------------------ rkp verify
#
# Reading and checking a bundle lives in repo_intel.knowledge.bundle_check; these commands
# parse arguments, call it and print.


def _add_verify_command(commands: Any) -> None:
    verify = commands.add_parser("verify", help="re-check .rkp/knowledge/ against its digests and the publish gate, without compiling")
    verify.add_argument("path", nargs="?", default=".", help="a directory inside the repository, its .rkp/, or a knowledge/ directory (default: .)")
    verify.add_argument("--json", action="store_true", help="one JSON object on stdout")


def _verify(args: argparse.Namespace) -> int:
    """`rkp verify`: exit 0 when the bundle is consistent and passes the publish gate, else 1."""
    from repo_intel.knowledge import bundle_check

    try:
        source = bundle_check.directory_source(args.path)
        try:
            problems, checked = bundle_check.verify_bundle(bundle_check.load_bundle(source))
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
        behaviours = f"{checked['behaviors']} behaviour units, " if checked.get("behaviors") else ""
        print(f"clean: {checked['repository_id']} at {checked['commit_sha'][:12]}, {checked['tier']} tier")
        print(f"  {checked['units']} units, {checked['entities']} entities, {checked['assertions']} assertions, {checked['evidence']} evidence rows, {behaviours}{checked['files']} files")
        print(f"  every digest matches and the publish gate passes; content hash {checked['content_hash']}")
    return 1 if problems else 0


# ------------------------------------------------------------------ rkp diff


def _add_diff_command(commands: Any) -> None:
    diff = commands.add_parser("diff", help="structural difference between two bundles: two directories, or two commits of one repository")
    diff.add_argument("a", help="the older bundle: a directory containing .rkp/, or a commit, branch or tag")
    diff.add_argument("b", help="the newer bundle, named the same way")
    diff.add_argument("--repo", default=".", help="the repository whose commits A and B name, when they are not directories (default: .)")
    diff.add_argument("--json", action="store_true", help="one JSON object on stdout")


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
    entities, relations, behaviours, coverage, files = result["entities"], result["relations"], result["behaviors"], result["coverage"], result["files"]
    s = entities["summary"]
    if not any(s[c] for c in ("added", "removed", "changed", "moved")) and not any(relations["summary"].values()) and not behaviours["rows"] and not coverage["entries"] and not any(files.values()) and not result["counts"]:
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
    if behaviours["rows"]:
        b = behaviours["summary"]
        print(f"behaviour: {b['added']} owners gained it, {b['removed']} lost it, {b['changed']} changed")
        for row in behaviours["rows"]:
            mark = {"added": "+", "removed": "-", "changed": "~"}[row["change"]]
            print(f"  {mark} {row['owner']['kind']} {row['owner']['name']}: {row['units_before']} -> {row['units_after']} units")
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
    from repo_intel.knowledge import bundle_check

    try:
        older = bundle_check.load_bundle(bundle_check.named_source(args.a, args.repo))
        newer = bundle_check.load_bundle(bundle_check.named_source(args.b, args.repo))
        result = bundle_check.diff_bundles(older, newer)
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
