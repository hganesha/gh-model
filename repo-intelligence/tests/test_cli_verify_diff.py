"""`rkp verify` and `rkp diff` (repo_intel.cli): re-checking a committed bundle, and comparing two."""

import json
import shutil
from pathlib import Path

import pytest

from repo_intel import cli
from repo_intel.compile.fixtures import _git, materialize
from repo_intel.knowledge import bundle
from repo_intel.storage.hashing import digest

ROUTE = 'from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n@router.get("/extra")\ndef extra():\n    return 1\n'


def _commit_all(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message, date="2026-02-01T00:00:00+00:00")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    """F01 compiled twice, each bundle committed after the source it describes: the base, then
    with one more route. (repo, [(source commit, bundle commit), ...])."""
    repo = tmp_path_factory.mktemp("verify") / "repo"
    materialize("F01-python-api", repo)
    commits = []
    for step in ("base", "extra"):
        if step == "extra":
            (repo / "app" / "extra.py").write_text(ROUTE)
            _commit_all(repo, "add a route")
        source = bundle.compile_bundle(repo)["commit_sha"]
        commits.append((source, _commit_all(repo, f"knowledge: {step}")))
    return repo, commits


@pytest.fixture
def repo(compiled, tmp_path):
    """A private copy of the compiled repository, with the newer bundle in its working tree."""
    copy = tmp_path / "repo"
    shutil.copytree(compiled[0], copy, symlinks=True)
    return copy


def _knowledge(repo):
    return repo / ".rkp" / "knowledge"


def _manifest(repo):
    return json.loads((_knowledge(repo) / "manifest.json").read_text())


def _write_manifest(repo, document):
    (_knowledge(repo) / "manifest.json").write_text(json.dumps(document, sort_keys=True, indent=2) + "\n")


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_lines(path, rows):
    path.write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows))


def _largest_unit(repo):
    return max(_manifest(repo)["units"], key=lambda u: u["entities"])


def _verify(capsys, *args):
    code = cli.main(["verify", *map(str, args), "--json"])
    out = json.loads(capsys.readouterr().out)
    return code, out


# ------------------------------------------------------------------ verify


def test_a_fresh_bundle_verifies_clean(repo, capsys):
    assert cli.main(["verify", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("clean: local/repo at ") and "architecture tier" in out and "publish gate passes" in out

    code, result = _verify(capsys, repo / ".rkp")  # the .rkp/ directory names the same bundle
    assert code == 0 and result["event"] == "result" and result["ok"] is True and result["problems"] == []
    checked = result["checked"]
    assert checked["tier"] == "architecture" and checked["units"] == len(_manifest(repo)["units"])
    assert checked["entities"] == _manifest(repo)["counts"]["entities"]
    assert "not re-derivable" in checked["content_hash"]


def test_a_full_bundle_re_derives_its_content_hash(repo, capsys):
    config = repo / ".rkp" / "config.json"
    config.write_text(json.dumps({**json.loads(config.read_text()), "commit": "full"}))
    bundle.compile_bundle(repo)
    shutil.rmtree(repo / ".rkp" / "cache")  # verify never needs the cache
    code, result = _verify(capsys, repo)
    assert code == 0, result["problems"]
    assert result["checked"]["tier"] == "full" and result["checked"]["content_hash"] == "re-derived from the rows"

    # In the full tier the snapshot's own digests are the reference: an edited row breaks
    # the unit's digest and the content hash.
    unit = _largest_unit(repo)
    path = _knowledge(repo) / unit["file"]
    rows = _lines(path)
    entity = next(r for r in rows if r["table"] == "entity")
    entity["attributes"] = {**entity.get("attributes", {}), "note": "edited by hand"}
    _write_lines(path, rows)
    code, result = _verify(capsys, repo)
    assert code == 1
    assert f"{unit['file']}: rows do not hash to the unit's digest in the snapshot record" in result["problems"]
    assert "publish gate: content_hash does not re-derive from shard rows" in result["problems"]


def test_an_edited_row_is_caught_even_with_a_warm_cache(repo, capsys):
    # The cache already holds every object; hydrate would accept the edit, verify must not.
    unit = _largest_unit(repo)
    path = _knowledge(repo) / unit["file"]
    rows = _lines(path)
    entity = next(r for r in rows if r["table"] == "entity")
    entity["name"] = entity["name"] + "-renamed"
    _write_lines(path, rows)
    code, result = _verify(capsys, repo)
    assert code == 1 and result["ok"] is False
    assert f"{unit['file']}: rows do not hash to the unit's digest in manifest.json" in result["problems"]

    assert cli.main(["verify", str(repo)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("1 problem in ") and unit["file"] in out


def test_missing_stray_and_miscounted_unit_files(repo, capsys):
    document = _manifest(repo)
    unit = _largest_unit(repo)
    (_knowledge(repo) / unit["file"]).rename(_knowledge(repo) / "units" / "left-over.jsonl")
    code, result = _verify(capsys, repo)
    problems = result["problems"]
    assert code == 1
    assert f"{unit['file']}: named by the manifest but missing" in problems
    assert "units/left-over.jsonl: not named by the manifest" in problems
    assert f"manifest.json: counts.entities is {document['counts']['entities']}; the files hold {document['counts']['entities'] - unit['entities']}" in problems


def test_a_unit_file_outside_the_bundle_is_never_read(repo, capsys):
    document = _manifest(repo)
    document["units"][0]["file"] = "../../app/main.py"
    _write_manifest(repo, document)
    code, result = _verify(capsys, repo)
    assert code == 1
    assert any("which is not a file under units/" in p for p in result["problems"])


def test_row_defaults_and_tier_must_be_consistent(repo, capsys):
    unit = _largest_unit(repo)
    path = _knowledge(repo) / unit["file"]
    rows = _lines(path)
    spelled = next(r for r in rows if r["table"] == "evidence")
    spelled["secret_masked"] = False  # the default, which format 2 leaves out: same meaning, other bytes
    _write_lines(path, rows)
    document = _manifest(repo)
    document["row_defaults"]["unknown"] = {"status": "resolved"}
    del document["detail_kinds"]
    _write_manifest(repo, document)
    code, result = _verify(capsys, repo)
    problems = result["problems"]
    assert code == 1
    assert f"{unit['file']}: 1 row spells out a default value, which rkp-bundle/2 leaves out" in problems
    assert "manifest.json: row_defaults differ from the ones rkp-bundle/2 fixes" in problems
    assert "manifest.json: detail_kinds is None; the architecture tier leaves out ['Symbol']" in problems
    # The row with the default spelled out still hashes the same: only the format rule is broken.
    assert not [p for p in problems if "do not hash" in p]


def test_a_bundle_relabelled_as_full_is_refused(repo, capsys):
    document = _manifest(repo)
    document["tier"] = "full"
    _write_manifest(repo, document)
    code, result = _verify(capsys, repo)
    problems = result["problems"]
    assert code == 1
    assert "manifest.json: a full bundle names detail kinds it left out" in problems
    assert any("rows do not hash to the unit's digest in the snapshot record" in p for p in problems)


def test_snapshot_hashes_must_re_derive(repo, capsys):
    document = _manifest(repo)
    document["snapshot"]["provenance_hash"] = digest("something else")
    _write_manifest(repo, document)
    code, result = _verify(capsys, repo)
    assert code == 1
    assert "manifest.json: provenance_hash does not re-derive from the toolchain, decisions and overrides" in result["problems"]
    assert "manifest.json: rkp_hash does not re-derive from the content and provenance hashes" in result["problems"]


def test_the_hydrated_rows_must_pass_the_publish_gate(repo, capsys):
    # files.jsonl carries no digest of its own: dropping a file the evidence cites is caught
    # by the gate, which looks up every evidence row's blob.
    files = _knowledge(repo) / "files.jsonl"
    rows = _lines(files)
    unit = _largest_unit(repo)
    cited = {r["blob_sha256"] for r in _lines(_knowledge(repo) / unit["file"]) if r["table"] == "evidence"}
    dropped = [r for r in rows if r.get("blob_sha256") in cited][0]
    _write_lines(files, [r for r in rows if r is not dropped])
    code, result = _verify(capsys, repo)
    problems = result["problems"]
    assert code == 1
    assert any(p.startswith("publish gate: evidence ") and p.endswith("blob not in the snapshot manifest") for p in problems)
    assert any(p.startswith("manifest.json: counts.files is ") for p in problems)


def test_repository_level_files_must_match_the_snapshot(repo, capsys):
    coverage = _knowledge(repo) / "coverage.jsonl"
    rows = _lines(coverage)
    rows[0]["numerator"] = rows[0]["denominator"] + 1
    _write_lines(coverage, rows)
    (_knowledge(repo) / "decisions.jsonl").unlink()
    code, result = _verify(capsys, repo)
    problems = result["problems"]
    assert code == 1
    assert "publish gate: coverage rows do not hash to coverage_ref" in problems
    assert any(p.startswith(f"publish gate: coverage {rows[0]['domain']}/") and p.endswith("numerator exceeds denominator") for p in problems)
    assert "decisions.jsonl: missing" in problems


def test_no_bundle_and_outside_a_repository(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    _git(plain, "init", "-q")
    code, result = _verify(capsys, plain)
    assert code == 1 and result["ok"] is False and "run `rkp compile` first" in result["problems"][0]

    outside = tmp_path / "outside"
    outside.mkdir()
    assert cli.main(["verify", str(outside), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["event"] == "error"
    assert cli.main(["verify", str(outside)]) == 1
    assert capsys.readouterr().err.startswith("rkp: ")


# ------------------------------------------------------------------ diff


def _diff(capsys, *args):
    code = cli.main(["diff", *map(str, args), "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_diff_between_two_commits_reads_the_committed_bundles(compiled, capsys):
    repo, [(base_source, base), (extra_source, extra)] = compiled
    head = _git(repo, "rev-parse", "HEAD")
    status = _git(repo, "status", "--porcelain")
    code, result = _diff(capsys, "HEAD~2", "HEAD", "--repo", repo)
    assert code == 0 and result["event"] == "result"
    assert result["from"]["commit_sha"] == base_source and result["to"]["commit_sha"] == extra_source
    assert result["from"]["bundle"] == f"HEAD~2 ({base[:12]})"
    assert result["same_repository"] is True and result["identical"] is False and result["warnings"] == []

    added = [r for r in result["entities"]["rows"] if r["change"] == "added"]
    assert {"Endpoint", "Artifact"} <= {r["kind"] for r in added}
    endpoint = next(r for r in added if r["kind"] == "Endpoint")
    assert "/extra" in endpoint["name"] and endpoint["plane"] == "contracts"
    assert result["entities"]["by_kind"]["Endpoint"]["added"] == 1
    assert result["entities"]["summary"]["removed"] == 0
    assert result["relations"]["summary"]["added"] > 0 and result["relations"]["summary"]["removed"] == 0
    assert sum(n["added"] for n in result["relations"]["by_predicate"].values()) == result["relations"]["summary"]["added"]
    assert any(r["object"]["kind"] == "Endpoint" for r in result["relations"]["rows"])
    assert result["files"]["added"] == ["app/extra.py"] and result["files"]["removed"] == []
    assert not [r for r in result["entities"]["rows"] if r["kind"] == "Symbol"], "the architecture tier has no Symbols"

    # Read with `git cat-file`: nothing was checked out or changed.
    assert _git(repo, "rev-parse", "HEAD") == head and _git(repo, "status", "--porcelain") == status

    assert cli.main(["diff", base, extra, "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"a: {base} ({base[:12]})") and "Endpoint" in out and "files: 1 added, 0 removed" in out


def test_diff_between_two_directories(compiled, tmp_path, capsys):
    repo, [(base_source, base), _] = compiled
    older = tmp_path / "older" / "repo"
    shutil.copytree(repo, older, symlinks=True)
    _git(older, "checkout", "-q", base)
    code, result = _diff(capsys, older, repo)
    assert code == 0 and result["same_repository"] is True
    assert result["from"]["commit_sha"] == base_source and result["entities"]["by_kind"]["Endpoint"]["added"] == 1

    # The same bundle on both sides: nothing differs.
    code, result = _diff(capsys, repo, repo / ".rkp")
    assert code == 0 and result["identical"] is True
    assert result["entities"]["summary"] == {"added": 0, "removed": 0, "changed": 0, "moved": 0, "evidence_only": 0}
    assert result["relations"]["rows"] == [] and result["coverage"] == {"domains": [], "entries": []}
    assert cli.main(["diff", str(repo), str(repo)]) == 0
    assert "No structural differences." in capsys.readouterr().out


def test_diff_compares_a_full_bundle_with_an_architecture_one_at_the_architecture_tier(compiled, tmp_path, capsys):
    repo, _ = compiled
    full = tmp_path / "full" / "repo"
    shutil.copytree(repo, full, symlinks=True)
    config = full / ".rkp" / "config.json"
    config.write_text(json.dumps({**json.loads(config.read_text()), "commit": "full"}))
    bundle.compile_bundle(full)
    assert _manifest(full)["counts"]["entities"] > _manifest(repo)["counts"]["entities"]
    code, result = _diff(capsys, repo, full)
    assert code == 0 and result["compared_tier"] == "architecture"
    assert result["entities"]["summary"]["added"] == result["entities"]["summary"]["removed"] == 0
    assert result["relations"]["summary"] == {"added": 0, "removed": 0, "support_changed": 0}
    assert cli.main(["diff", str(repo), str(full)]) == 0
    assert "compared at the architecture tier" in capsys.readouterr().out


def test_diff_matches_two_repositories_by_kind_and_natural_key(compiled, tmp_path, capsys):
    repo, _ = compiled
    other = tmp_path / "other" / "repo"
    shutil.copytree(repo, other, symlinks=True)
    config = other / ".rkp" / "config.json"
    config.write_text(json.dumps({**json.loads(config.read_text()), "repository_id": "example.com/acme/orders"}))
    bundle.compile_bundle(other)
    code, result = _diff(capsys, repo, other)
    assert code == 0 and result["same_repository"] is False and result["identical"] is False
    # Every ID differs (it folds in the repository ID), yet the same things are matched.
    assert result["relations"]["summary"]["added"] == result["relations"]["summary"]["removed"] == 0
    [renamed] = result["entities"]["rows"]
    assert renamed["kind"] == "Repository" and renamed["change"] == "changed" and renamed["changed_fields"] == ["name"]
    assert cli.main(["diff", str(repo), str(other)]) == 0
    assert "two repositories: entities are matched by kind and natural key" in capsys.readouterr().out


def test_diff_reports_a_damaged_side_as_a_warning(compiled, tmp_path, capsys):
    repo, _ = compiled
    damaged = tmp_path / "damaged"
    shutil.copytree(repo, damaged, symlinks=True)
    (damaged / ".rkp" / "knowledge" / "unknowns.jsonl").unlink()
    code, result = _diff(capsys, repo, damaged)
    assert code == 0 and any(w.endswith("unknowns.jsonl: missing") for w in result["warnings"])


def test_diff_errors(compiled, tmp_path, capsys):
    repo, [(base_source, _), _] = compiled
    code, result = _diff(capsys, base_source, "HEAD", "--repo", repo)  # the source commit, before its bundle
    assert code == 1 and result["event"] == "error" and "no bundle in" in result["message"]
    code, result = _diff(capsys, "no-such-branch", "HEAD", "--repo", repo)
    assert code == 1 and result["event"] == "error"
    assert cli.main(["diff", "--repo", str(repo), "--", "-x", "HEAD"]) == 1
    assert "cannot start with '-'" in capsys.readouterr().err


def test_coverage_changes_name_platform_constructs():
    def entry(seen_lock, modelled_lock, not_seen):
        constructs = {"cf:cflock": {"seen": seen_lock, "modelled": modelled_lock}}
        return {"domain": "platform_constructs", "scope": "cfml", "numerator": modelled_lock, "denominator": seen_lock, "detail": {"constructs": constructs, "not_seen": not_seen}}

    parse = {"domain": "parse", "scope": ".", "numerator": 3, "denominator": 4, "detail": {}}
    before = [parse, entry(4, 0, ["cf:cfthread"])]
    after = [{**parse, "numerator": 4}, {**entry(4, 4, []), "detail": {"constructs": {"cf:cflock": {"seen": 4, "modelled": 4}, "cf:cfthread": {"seen": 1, "modelled": 0}}, "not_seen": []}, "denominator": 5}]
    changes = cli._coverage_changes(before, after)
    assert [d["domain"] for d in changes["domains"]] == ["parse", "platform_constructs"]
    cfml = next(e for e in changes["entries"] if e["domain"] == "platform_constructs")
    assert cfml["change"] == "changed" and cfml["before"] == {"numerator": 0, "denominator": 4} and cfml["after"] == {"numerator": 4, "denominator": 5}
    assert cfml["constructs"] == {"cf:cflock": {"before": {"seen": 4, "modelled": 0}, "after": {"seen": 4, "modelled": 4}}, "cf:cfthread": {"before": None, "after": {"seen": 1, "modelled": 0}}}
    assert cfml["now_seen"] == ["cf:cfthread"] and cfml["no_longer_seen"] == []
    gone = cli._coverage_changes([entry(1, 1, [])], [])
    assert gone["entries"][0]["change"] == "removed" and gone["entries"][0]["no_longer_seen"] == ["cf:cflock"]
    assert cli._coverage_changes(before, before) == {"domains": [], "entries": []}
