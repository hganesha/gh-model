"""`rkp verify` and `rkp diff` (repo_intel.cli, repo_intel.knowledge.bundle_check): re-checking a
committed bundle, and comparing two."""

import json
import shutil
from pathlib import Path

import pytest

from repo_intel import cli, extractors, platforms
from repo_intel.compile import behavior, buildgraph
from repo_intel.compile.fixtures import _git, materialize
from repo_intel.compile.ontology import EntityKind
from repo_intel.knowledge import bundle, bundle_check
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


CONTRACT = "openapi: 3.0.0\ninfo:\n  title: Orders\n  version: 1\npaths:\n  /orders:\n{ops}"
OPERATION = "    {method}:\n      operationId: {name}\n      responses:\n        '200':\n          description: OK\n"


def test_diff_compares_the_exports(tmp_path, capsys):
    repo = tmp_path / "web"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "package.json").write_text('{"name": "web", "version": "1.0.0", "dependencies": {"axios": "1.7.0"}}\n')
    versions = [
        (OPERATION.format(method="get", name="listOrders") + OPERATION.format(method="post", name="placeOrder"),
         'import axios from "axios";\nexport const a = () => fetch("/api/customers");\nexport const b = () => axios.post("/api/customers", {});\n'),
        (OPERATION.format(method="get", name="listOrders") + OPERATION.format(method="put", name="replaceOrders"),
         'import axios from "axios";\nexport const a = () => fetch("/api/customers");\nexport const c = () => axios.delete("/api/invoices");\n'),
    ]
    for contract, source in versions:
        (repo / "openapi.yaml").write_text(CONTRACT.format(ops=contract))
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "api.ts").write_text(source)
        _commit_all(repo, "source")
        bundle.compile_bundle(repo)
        _commit_all(repo, "knowledge")

    code, result = _diff(capsys, "HEAD~2", "HEAD", "--repo", repo)
    assert code == 0 and result["warnings"] == []
    exports = result["exports"]
    assert exports["compared"] is True and exports["summary"] == {"added": 2, "removed": 2}
    assert [(r["change"], r["method"], r["route"], r["interface"]["kind"]) for r in exports["operations"]] == [
        ("removed", "POST", "/orders", "Interface"), ("added", "PUT", "/orders", "Interface")]
    assert [(r["change"], r["method"], r["path"]) for r in exports["unresolved_calls"]] == [
        ("removed", "POST", "/api/customers"), ("added", "DELETE", "/api/invoices")]
    assert all(r["caller"] and r["caller"]["id"].startswith("ent_") for r in exports["unresolved_calls"])

    # The same bundle on both sides: nothing added or removed.
    code, result = _diff(capsys, "HEAD", repo, "--repo", repo)
    assert result["exports"] == {"compared": True, "summary": {"added": 0, "removed": 0}, "operations": [], "unresolved_calls": []}

    # A bundle written before exports.json existed: the exports are not compared.
    old = tmp_path / "old"
    shutil.copytree(_knowledge(repo), old)
    document = json.loads((old / "manifest.json").read_text())
    del document["exports"]
    (old / "manifest.json").write_text(json.dumps(document))
    (old / bundle.EXPORTS_FILE).unlink()
    code, result = _diff(capsys, old, repo)
    assert code == 0 and result["exports"] == {"compared": False, "summary": {"added": 0, "removed": 0}, "operations": [], "unresolved_calls": []}


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
    changes = bundle_check.coverage_changes(before, after)
    assert [d["domain"] for d in changes["domains"]] == ["parse", "platform_constructs"]
    cfml = next(e for e in changes["entries"] if e["domain"] == "platform_constructs")
    assert cfml["change"] == "changed" and cfml["before"] == {"numerator": 0, "denominator": 4} and cfml["after"] == {"numerator": 4, "denominator": 5}
    assert cfml["constructs"] == {"cf:cflock": {"before": {"seen": 4, "modelled": 0}, "after": {"seen": 4, "modelled": 4}}, "cf:cfthread": {"before": None, "after": {"seen": 1, "modelled": 0}}}
    assert cfml["now_seen"] == ["cf:cfthread"] and cfml["no_longer_seen"] == []
    gone = bundle_check.coverage_changes([entry(1, 1, [])], [])
    assert gone["entries"][0]["change"] == "removed" and gone["entries"][0]["no_longer_seen"] == ["cf:cflock"]
    assert bundle_check.coverage_changes(before, before) == {"domains": [], "entries": []}


# ------------------------------------------------------------------ bundles with behaviour
#
# A demo pack, as in tests/test_behavior_store.py: a `.demo` file is a JSON document naming a
# program, its page body and its functions; each function is a Symbol with one behaviour unit.


class DemoSource:
    id, version = "demo.source", "1.0.0"

    def matches(self, path):
        return path.endswith(".demo")

    def extract(self, path, data, blob):
        from repo_intel.extractors.base import Extraction, entity, find_span, ref, whole_file

        doc = json.loads(data)
        program = ref(EntityKind.PROGRAM, {"platform": "demo", "name": doc["program"]}, doc["program"])
        out = [platforms.platform_entity(blob, whole_file(data), "demo:Template", program["natural_key"], program["name"])]
        out.append(behavior.behavior_observation(blob, find_span(data, '"page"'), program, "demo", doc["page"]))
        for name, body in sorted(doc["functions"].items()):
            owner = ref(EntityKind.SYMBOL, {"platform": "demo", "program": doc["program"], "name": name}, name)
            where = find_span(data, f'"{name}"')
            out.append(entity(blob, where, EntityKind.SYMBOL, owner["natural_key"], name))
            out.append(behavior.behavior_observation(blob, where, owner, "demo", body))
        return Extraction(observations=out, parsed=True)


DEMO = platforms.PlatformPack(
    id="demo", name="Demo platform", prefix="demo", version="1.0.0",
    terms=(platforms.Term("demo:Template", EntityKind.PROGRAM, "lossy", "a page template with code"),),
    extractors=(DemoSource(),),
)
LIT = lambda v: {"e": "lit", "value": v, "type": "string"}  # noqa: E731
PAGE = [{"op": "output", "at": [1, 2], "channel": "response", "value": LIT("orders")}]
SAVE = [{"op": "query", "at": [3, 4], "sql": "UPDATE orders SET qty = :qty", "datasource": "shopdsn",
         "params": [{"name": "qty", "value": {"e": "var", "scope": "form", "name": "qty"}, "type": "integer"}]}]
DEMO_SOURCE = {"program": "orders", "page": PAGE, "functions": {"save": SAVE, "lookup": [{"op": "return", "at": [1, 2], "value": LIT("x")}]}}


@pytest.fixture
def demo(monkeypatch):
    registry = platforms._load()
    monkeypatch.setitem(registry, "PACKS", {"demo": DEMO})
    monkeypatch.setitem(registry, "TERMS", {t.name: (DEMO, t) for t in DEMO.terms})
    monkeypatch.setitem(registry, "CONSTRUCTS", {})
    monkeypatch.setattr(extractors, "EXTRACTORS", sorted([*extractors.EXTRACTORS, *DEMO.extractors], key=lambda e: e.id))
    # The pack exists only in this process, so steps must run here, not in a sandboxed worker.
    monkeypatch.setenv(buildgraph.EXECUTOR_ENV, "inprocess")
    yield DEMO
    extractors._EXTRACTOR_VERSIONS.clear()


def _demo_repo(path, tier, source=DEMO_SOURCE):
    """A repository with one `.demo` file whose bundle is compiled in `tier` and committed."""
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "orders.demo").write_text(json.dumps(source, indent=1, sort_keys=True))
    bundle.ensure_layout(path)
    config = path / ".rkp" / "config.json"
    config.write_text(json.dumps({**json.loads(config.read_text()), "commit": tier}))
    _commit_all(path, "demo")
    bundle.compile_bundle(path)
    _commit_all(path, "knowledge")
    return path


def _behaviour_unit(repo):
    """The manifest entry of the unit holding behaviour rows, and its rows."""
    unit = next(u for u in _manifest(repo)["units"] if u.get("behaviors"))
    return unit, _lines(_knowledge(repo) / unit["file"])


def test_the_explorer_labels_every_coverage_domain_the_engine_writes():
    import re

    engine = Path(__file__).resolve().parents[1]
    # Bundle coverage is built as CoverageEntry(domain="...") in the compiler and the linker.
    written = {m for p in (engine / "repo_intel" / "compile").glob("*.py") for m in re.findall(r'CoverageEntry\(\s*domain="([a-z_]+)"', p.read_text())}
    assert {"parse", "inventory", "behavior", "platform_constructs"} <= written
    planes = (engine / "explorer" / "lib" / "planes.ts").read_text()
    table = planes[planes.index("export const DOMAIN_LABEL"):]
    labelled = set(re.findall(r"^\s+([a-z_]+): \"", table[:table.index("};")], re.MULTILINE))
    assert written - labelled == set(), "explorer/lib/planes.ts DOMAIN_LABEL lacks these domains"


def test_row_names_cover_every_table_the_bundle_writes():
    assert set(bundle_check.ROW_NAMES) == {*bundle.ROW_TABLES, *bundle.OPTIONAL_TABLES}
    assert bundle_check.ROW_NAMES == dict(bundle._ALL_TABLES)


def test_a_full_bundle_with_behaviour_verifies_clean(demo, tmp_path, capsys):
    repo = _demo_repo(tmp_path / "repo", "full")
    shutil.rmtree(repo / ".rkp" / "cache")  # verify never needs the cache
    code, result = _verify(capsys, repo)
    assert code == 0, result["problems"]
    assert result["checked"]["tier"] == "full" and result["checked"]["behaviors"] == 3
    assert result["checked"]["content_hash"] == "re-derived from the rows"
    assert cli.main(["verify", str(repo)]) == 0
    assert "3 behaviour units" in capsys.readouterr().out


def test_damaged_behaviour_rows_are_named(demo, tmp_path, capsys):
    repo = _demo_repo(tmp_path / "repo", "full")
    unit, rows = _behaviour_unit(repo)
    behaviours = [r for r in rows if r["table"] == "behavior"]
    behaviours[0]["owner"] = "ent_nobody"
    behaviours[1]["body"] = [{"op": "nonsense"}]
    rows.remove(behaviours[2])
    rows.append({"table": "mystery", "id": "x"})
    _write_lines(_knowledge(repo) / unit["file"], rows)
    code, result = _verify(capsys, repo)
    problems = result["problems"]
    assert code == 1
    file = unit["file"]
    assert f"{file}: behavior {behaviours[0]['id']}: owner ent_nobody is not a Program or Symbol of this unit" in problems
    assert any(p.startswith(f"{file}: behavior {behaviours[1]['id']}: not a valid behaviour unit (") for p in problems)
    assert f"{file}: 2 behaviors rows; the manifest says 3" in problems
    assert "manifest.json: counts.behaviors is 3; the files hold 2" in problems
    assert f"{file}: 1 row names table 'mystery', which format rkp-bundle/2 does not have" in problems
    assert f"{file}: rows do not hash to the unit's digest in the snapshot record" in problems


def test_the_architecture_tier_must_leave_behaviour_out(demo, tmp_path, capsys):
    full = _demo_repo(tmp_path / "full", "full")
    repo = _demo_repo(tmp_path / "repo", "architecture")
    code, result = _verify(capsys, repo)
    assert code == 0, result["problems"]
    assert result["checked"]["behaviors"] == 0 and _manifest(repo)["detail_omitted"]["behaviors"] == 3

    # Behaviour rows put back by hand: detail the tier leaves out, and a broken digest.
    _, rows = _behaviour_unit(full)
    unit = _largest_unit(repo)
    path = _knowledge(repo) / unit["file"]
    _write_lines(path, _lines(path) + [r for r in rows if r["table"] == "behavior"])
    document = _manifest(repo)
    document["detail_tables"] = ["behaviors", "secrets"]
    _write_manifest(repo, document)
    code, result = _verify(capsys, repo)
    problems = result["problems"]
    assert code == 1
    assert "the architecture tier holds detail it should leave out: 3 behaviors rows" in problems
    assert "manifest.json: detail_tables is ['behaviors', 'secrets']; the architecture tier leaves out only tables among ['behaviors', 'transforms']" in problems
    assert f"{unit['file']}: rows do not hash to the unit's digest in manifest.json" in problems


def test_an_architecture_bundle_written_before_a_detail_table_existed_still_verifies(demo, tmp_path, capsys):
    # Written before transform shapes: detail_tables names only behaviour.
    repo = _demo_repo(tmp_path / "repo", "architecture")
    document = _manifest(repo)
    document["detail_tables"] = ["behaviors"]
    del document["detail_omitted"]["transforms"]
    _write_manifest(repo, document)
    code, result = _verify(capsys, repo)
    assert code == 0, result["problems"]

    for wrong in ("behaviors", [["behaviors"]], ["secrets"]):  # not a list of known table names
        document["detail_tables"] = wrong
        _write_manifest(repo, document)
        code, result = _verify(capsys, repo)
        assert code == 1 and f"manifest.json: detail_tables is {wrong!r}; the architecture tier leaves out only tables among ['behaviors', 'transforms']" in result["problems"]

    # The rows are checked whatever the list says: behaviour put back is still refused.
    document["detail_tables"] = ["behaviors"]
    _write_manifest(repo, document)
    full = _demo_repo(tmp_path / "full", "full")
    _, rows = _behaviour_unit(full)
    path = _knowledge(repo) / _largest_unit(repo)["file"]
    _write_lines(path, _lines(path) + [r for r in rows if r["table"] == "behavior"])
    code, result = _verify(capsys, repo)
    assert code == 1 and "the architecture tier holds detail it should leave out: 3 behaviors rows" in result["problems"]


def test_a_full_bundle_from_a_real_pack_verifies_clean(tmp_path, capsys):
    # F20's ColdFusion pack emits behaviour for every function and page; its full tier once
    # failed verify with one "a row names table 'behavior'" problem per unit of behaviour.
    repo = tmp_path / "f20"
    materialize("F20-coldfusion-app", repo)
    bundle.ensure_layout(repo)
    config = repo / ".rkp" / "config.json"
    config.write_text(json.dumps({**json.loads(config.read_text()), "commit": "full"}))
    document = bundle.compile_bundle(repo)
    assert document["counts"]["behaviors"] > 0
    code, result = _verify(capsys, repo)
    assert code == 0, result["problems"]
    assert result["checked"]["behaviors"] == document["counts"]["behaviors"]


def test_diff_between_full_bundles_with_behaviour(demo, tmp_path, capsys):
    repo = _demo_repo(tmp_path / "repo", "full")
    shifted = [{**SAVE[0], "at": [30, 40]}]  # the same statement at other bytes: not a change
    changed = {**DEMO_SOURCE, "functions": {"save": shifted, "lookup": [{"op": "return", "at": [1, 2], "value": LIT("y")}], "cancel": [{"op": "exit", "at": [1, 2]}]}}
    (repo / "orders.demo").write_text(json.dumps(changed, indent=1, sort_keys=True))
    _commit_all(repo, "change lookup, add cancel")
    bundle.compile_bundle(repo)
    _commit_all(repo, "knowledge again")

    code, result = _diff(capsys, "HEAD~2", "HEAD", "--repo", repo)
    assert code == 0 and result["warnings"] == [], "behaviour rows are not damage"
    assert result["compared_tier"] == "full" and result["behaviors"]["compared"] is True
    assert result["behaviors"]["summary"] == {"added": 1, "removed": 0, "changed": 1}
    assert [(r["change"], r["owner"]["kind"], r["owner"]["name"], r["units_before"], r["units_after"]) for r in result["behaviors"]["rows"]] == [
        ("added", "Symbol", "cancel", 0, 1),
        ("changed", "Symbol", "lookup", 1, 1),
    ]
    assert result["entities"]["by_kind"]["Symbol"]["added"] == 1

    assert cli.main(["diff", "HEAD~2", "HEAD", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "warning" not in out and "behaviour: 1 owners gained it, 0 lost it, 1 changed" in out and "~ Symbol lookup: 1 -> 1 units" in out

    # The same bundle on both sides, read from a directory and from the commit that holds it.
    code, result = _diff(capsys, repo, "HEAD", "--repo", repo)
    assert code == 0 and result["identical"] is True and result["behaviors"]["rows"] == [] and result["warnings"] == []

    # Against an architecture-tier bundle, behaviour is detail and is not compared.
    arch = _demo_repo(tmp_path / "arch", "architecture", changed)
    code, result = _diff(capsys, arch, repo)
    assert code == 0 and result["compared_tier"] == "architecture" and result["warnings"] == []
    assert result["behaviors"] == {"compared": False, "summary": {"added": 0, "removed": 0, "changed": 0}, "rows": []}
