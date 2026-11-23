"""Materialize a benchmark fixture as a real Git repository with stable SHAs.

A fixture is `base/` plus ordered overlay directories under `commits/`. Each
may carry `_ops.json`: {"message", "delete": [paths], "symlinks": {path: target},
"generate": {path: size_in_bytes}, "gitlinks": {path: commit_sha}}. `generate` writes
a deterministic filler file (for oversize tests) and `gitlinks` records a submodule
entry without fetching anything.
Author, committer and dates are fixed, so commit SHAs are reproducible.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "benchmarks" / "fixtures"

_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "PATH": os.environ.get("PATH", ""),
}


def _git(repo: Path, *args: str, date: str | None = None) -> str:
    env = dict(_ENV)
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    cmd = ["git", "-C", str(repo), "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false", *args]
    return subprocess.run(cmd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _apply(layer: Path, repo: Path) -> dict:
    ops = json.loads((layer / "_ops.json").read_text()) if (layer / "_ops.json").exists() else {}
    for src in sorted(p for p in layer.rglob("*") if p.is_file() and p.name != "_ops.json"):
        dst = repo / src.relative_to(layer)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    for rel in ops.get("delete", []):
        (repo / rel).unlink()
    for rel, target in ops.get("symlinks", {}).items():
        link = repo / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link)
    for rel, size in ops.get("generate", {}).items():
        out = repo / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        line = b"filler line for oversize fixture content\n"
        out.write_bytes((line * (size // len(line) + 1))[:size])
    ops.setdefault("message", layer.name)
    return ops


def materialize(fixture: str, dest: Path) -> list[str]:
    """Create the fixture repo at `dest`; return commit SHAs, base first."""
    src = FIXTURES_DIR / fixture
    dest.mkdir(parents=True, exist_ok=False)
    _git(dest, "init", "-q", "-b", "main")
    layers = [src / "base", *sorted((src / "commits").iterdir())] if (src / "commits").exists() else [src / "base"]
    shas = []
    for i, layer in enumerate(layers):
        ops = _apply(layer, dest)
        _git(dest, "add", "-A")
        for rel, sha in ops.get("gitlinks", {}).items():
            _git(dest, "update-index", "--add", "--cacheinfo", f"160000,{sha},{rel}")
        _git(dest, "commit", "-q", "-m", ops["message"], date=f"2026-01-01T00:{i:02d}:00+00:00")
        shas.append(_git(dest, "rev-parse", "HEAD"))
    return shas


def list_fixtures() -> list[str]:
    return sorted(p.name for p in FIXTURES_DIR.iterdir() if (p / "golden.json").exists())


def load_golden(fixture: str) -> dict:
    return json.loads((FIXTURES_DIR / fixture / "golden.json").read_text())
