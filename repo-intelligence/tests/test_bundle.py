"""The `.rkp/` knowledge bundle and the `rkp` command line (repo_intel.knowledge.bundle, repo_intel.cli)."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from repo_intel import cli
from repo_intel.compile.fixtures import _git, materialize
from repo_intel.compile import contracts
from repo_intel.compile.contracts import Manifest
from repo_intel.compile.pipeline import load_shards
from repo_intel.knowledge import bundle


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    materialize("F01-python-api", path)
    return path


def _files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _commit_all(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message, date="2026-02-01T00:00:00+00:00")


def test_compile_writes_a_complete_deterministic_bundle(repo):
    document = bundle.compile_bundle(repo)
    knowledge = repo / ".rkp" / "knowledge"
    assert document["bundle_format"] == "rkp-bundle/2"
    assert json.loads((knowledge / "manifest.json").read_text())["row_defaults"] == bundle.ROW_DEFAULTS
    assert document["repository_id"] == "local/repo"
    assert json.loads((repo / ".rkp" / "config.json").read_text())["repository_id"] == "local/repo"
    assert (repo / ".rkp" / ".gitignore").read_text().startswith("# Written by repo-intel")

    rows = [json.loads(line) for unit in document["units"] for line in (knowledge / unit["file"]).read_text().splitlines()]
    entities = [r for r in rows if r["table"] == "entity"]
    assert len(entities) == document["counts"]["entities"] > 0
    assert {"orders-api"} <= {e["name"] for e in entities}
    assert all(r["table"] in {"entity", "assertion", "evidence", "unknown"} for r in rows)
    # Format 2: a field holding its default is left out of the row.
    for r in rows:
        defaults = bundle.ROW_DEFAULTS[r["table"]]
        assert not [k for k in defaults if k in r and json.dumps(r[k]) == json.dumps(defaults[k])], r
    assert any("attributes" not in e for e in entities)

    # Evidence resolves to paths through files.jsonl, without any source text in the bundle.
    files = [json.loads(line) for line in (knowledge / "files.jsonl").read_text().splitlines()]
    by_blob = {f["blob_sha256"]: f["path"] for f in files if "blob_sha256" in f}
    evidence = [r for r in rows if r["table"] == "evidence"]
    assert evidence and all(e["blob_sha256"] in by_blob for e in evidence)
    assert all("text" not in e for e in evidence)

    first = _files(knowledge)
    bundle.compile_bundle(repo)
    assert _files(knowledge) == first


def test_status_is_missing_then_fresh_then_stale(repo):
    assert bundle.bundle_status(repo)["state"] == "missing"
    bundle.compile_bundle(repo)
    status = bundle.bundle_status(repo)
    assert status["state"] == "fresh", status["reasons"]
    assert status["uncommitted_changes"] is False

    (repo / "app" / "extra.py").write_text("def extra():\n    return 1\n")
    assert bundle.bundle_status(repo)["uncommitted_changes"] is True
    _commit_all(repo, "add extra")
    status = bundle.bundle_status(repo)
    assert status["state"] == "stale" and status["reasons"] == ["source changed"]


def test_committing_the_bundle_does_not_change_what_it_describes(repo):
    before = bundle.compile_bundle(repo)
    _commit_all(repo, "add knowledge")
    status = bundle.bundle_status(repo)
    assert status["state"] == "fresh", status["reasons"]
    assert status["bundle_commit_sha"] != status["commit_sha"]
    after = bundle.compile_bundle(repo)
    assert after["source_tree_hash"] == before["source_tree_hash"]
    assert after["units"] == before["units"]


def _git_state(repo):
    git = repo / ".git"
    return {str(p.relative_to(git)): p.stat().st_mtime_ns for p in git.rglob("*") if p.is_file()}


def test_working_tree_includes_uncommitted_changes_and_writes_nothing_to_git(repo):
    committed = bundle.compile_bundle(repo)
    (repo / "app" / "extra.py").write_text("from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n@router.get('/extra')\ndef extra():\n    return 1\n")
    (repo / "README.md").unlink(missing_ok=True)
    before = _git_state(repo)

    assert bundle.bundle_status(repo, working_tree=True)["state"] == "stale"
    document = bundle.compile_bundle(repo, working_tree=True)
    assert document["working_tree"] is True and document["commit_sha"] == committed["commit_sha"]
    assert document["source_tree_hash"] != committed["source_tree_hash"]
    files = {json.loads(line)["path"] for line in (repo / ".rkp" / "knowledge" / "files.jsonl").read_text().splitlines()}
    assert "app/extra.py" in files and "README.md" not in files
    status = bundle.bundle_status(repo, working_tree=True)
    assert status["state"] == "fresh", status["reasons"]
    assert status["uncommitted_changes"] is True
    # Against the commit alone, the bundle describes something else.
    assert bundle.bundle_status(repo)["state"] == "stale"
    assert _git_state(repo) == before

    # Once the same files are committed, the bundle describes the commit.
    _commit_all(repo, "add extra")
    status = bundle.bundle_status(repo)
    assert status["state"] == "fresh", status["reasons"]
    assert bundle.bundle_status(repo, working_tree=True)["state"] == "fresh"


def test_working_tree_matches_a_commit_compile_when_clean(repo):
    by_commit = bundle.compile_bundle(repo)
    by_tree = bundle.compile_bundle(repo, working_tree=True)
    for key in ("source_tree_hash", "units", "counts", "rkp_hash"):
        assert by_tree[key] == by_commit[key], key


def test_hydrate_rebuilds_the_cache_from_committed_files(repo):
    document = bundle.compile_bundle(repo)
    shutil.rmtree(repo / ".rkp" / "cache")
    store, manifest = bundle.hydrate(repo)
    assert manifest.rkp_hash == document["rkp_hash"]
    assert len(load_shards(store, manifest)) == len(document["units"])

    unit = repo / ".rkp" / "knowledge" / document["units"][0]["file"]
    unit.write_text(unit.read_text().replace('"', "'", 0) + "\n")  # whitespace only: still valid
    shutil.rmtree(repo / ".rkp" / "cache")
    bundle.hydrate(repo)
    lines = unit.read_text().splitlines()
    unit.write_text("\n".join(lines[1:]) + "\n")
    shutil.rmtree(repo / ".rkp" / "cache")
    with pytest.raises(bundle.BundleError, match="does not match"):
        bundle.hydrate(repo)


def test_hydrate_restores_omitted_fields_exactly(repo):
    document = bundle.compile_bundle(repo)
    compiled = load_shards(bundle.store_for(repo), Manifest.model_validate(document["snapshot"]))
    shutil.rmtree(repo / ".rkp" / "cache")
    store, manifest = bundle.hydrate(repo)
    assert load_shards(store, manifest) == compiled  # every digest verified, and the rows equal


def test_reads_a_format_1_bundle(repo):
    """Bundles committed before format 2 hold every field and no row_defaults."""
    document = bundle.compile_bundle(repo)
    knowledge = repo / ".rkp" / "knowledge"
    for unit in document["units"]:
        file = knowledge / unit["file"]
        rows = [json.loads(line) for line in file.read_text().splitlines()]
        full = [{**bundle.ROW_DEFAULTS[r["table"]], **r} for r in rows]
        file.write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in full))
    manifest_file = knowledge / "manifest.json"
    old = json.loads(manifest_file.read_text())
    old["bundle_format"] = "rkp-bundle/1"
    del old["row_defaults"]
    manifest_file.write_text(json.dumps(old))
    shutil.rmtree(repo / ".rkp" / "cache")
    store, manifest = bundle.hydrate(repo)
    assert manifest.rkp_hash == document["rkp_hash"]
    assert bundle.read_manifest(repo)["bundle_format"] == "rkp-bundle/1"
    assert bundle.bundle_status(repo)["reasons"] == ["bundle format changed"]


def test_row_defaults_agree_with_the_contracts():
    models = {"entity": contracts.Entity, "assertion": contracts.Assertion, "evidence": contracts.Evidence, "unknown": contracts.Unknown}
    for table, defaults in bundle.ROW_DEFAULTS.items():
        fields = models[table].model_fields
        for name, value in defaults.items():
            assert name in fields, (table, name)
            if not fields[name].is_required():
                assert fields[name].get_default(call_default_factory=True) == value, (table, name)


def test_overrides_file_is_part_of_the_bundle_key(repo):
    bundle.compile_bundle(repo)
    (repo / ".rkp" / "overrides.json").write_text(json.dumps({"decisions": [], "preferences": []}))
    assert bundle.bundle_status(repo)["state"] == "fresh"
    (repo / ".rkp" / "overrides.json").write_text("{not json")
    with pytest.raises(bundle.BundleError):
        bundle.bundle_status(repo)


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/hganesha/gh-model.git", "github.com/hganesha/gh-model"),
    ("git@github.com:hganesha/gh-model.git", "github.com/hganesha/gh-model"),
    ("ssh://git@gitlab.example.com/group/sub/repo", "gitlab.example.com/group/sub/repo"),
    ("https://token@dev.azure.com/org/project/_git/repo/", "dev.azure.com/org/project/_git/repo"),
    ("", None),
])
def test_repository_id_from_remote(url, expected):
    assert bundle._remote_id(url) == expected


def test_repository_id_prefers_origin_and_then_config(repo):
    _git(repo, "remote", "add", "origin", "git@github.com:acme/orders.git")
    assert bundle.default_repository_id(repo) == "github.com/acme/orders"
    (repo / ".rkp").mkdir()
    (repo / ".rkp" / "config.json").write_text(json.dumps({"repository_id": "acme/pinned"}))
    assert bundle.compile_bundle(repo)["repository_id"] == "acme/pinned"


def test_unit_file_names_are_unique():
    slugs = bundle._slugs([".", "@global", "apps/api", "apps-api", "root"])
    assert slugs["."] == "root" and slugs["@global"] == "global"
    assert len(set(slugs.values())) == 5


def test_cli_json_lines(repo, capsys):
    assert cli.main(["compile", str(repo), "--json"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0] == {"event": "progress", "stage": "start", "path": str(repo), "ref": "HEAD", "working_tree": False}
    assert {e["stage"] for e in events if e["event"] == "progress"} >= {"snapshot", "extract", "published", "written"}
    assert events[-1]["event"] == "result" and events[-1]["status"] == "complete"

    assert cli.main(["status", str(repo / "app"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "fresh"

    assert cli.main(["compile", str(repo), "--json", "--ref", "no-such-branch"]) == 1
    error = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert error["event"] == "error" and error["message"]


def test_cli_outside_a_repository(tmp_path, capsys):
    assert cli.main(["status", str(tmp_path)]) == 1
    assert "rkp:" in capsys.readouterr().err


def test_rkp_console_script(repo):
    rkp = shutil.which("rkp") or shutil.which("rkp", path=str(Path(sys.executable).parent))
    if rkp is None:
        pytest.skip("rkp is not installed")
    out = subprocess.run([rkp, "status", str(repo)], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.startswith("missing:")
