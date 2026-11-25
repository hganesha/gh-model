"""Command-line engine: compile a repository into its `.rkp/` knowledge bundle.

    rkp compile [PATH] [--ref REF] [--working-tree] [--json]
    rkp status  [PATH] [--ref REF] [--working-tree] [--json]

    rkp serve   [PATH] --stdio

`serve --stdio` answers the Explorer's read API from the bundle, one JSON request per
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
    args = parser.parse_args(argv)
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


if __name__ == "__main__":
    sys.exit(main())
