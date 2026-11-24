"""Every benchmark fixture: well-formed goldens, M0 expectations, replay and parity."""

import functools

import pytest

from conftest import REPO_ID
from repo_intel.compile.contracts import entity_id
from repo_intel.compile.fixtures import FIXTURES_DIR, list_fixtures, load_golden, materialize
from repo_intel.compile.ontology import EntityKind, check_relation
from repo_intel.compile.pipeline import compile_commit, load_shards
from repo_intel.storage.store import ObjectStore

FIXTURES = list_fixtures()
MILESTONES = {"M0", "M1", "M2", "M3", "M4", "M5"}


def _eid(item):
    return entity_id(REPO_ID, item["kind"], item["natural_key"])


@pytest.fixture(scope="session")
def compiled(tmp_path_factory):
    """Compile every commit of a fixture warm (one store) and cold (fresh store each)."""
    @functools.cache
    def run(name):
        root = tmp_path_factory.mktemp(name)
        shas = materialize(name, root / "repo")
        warm_store = ObjectStore(root / "warm", "t")
        out = []
        for i, sha in enumerate(shas):
            warm = compile_commit(warm_store, root / "repo", REPO_ID, sha)
            cold = compile_commit(ObjectStore(root / f"cold{i}", "t"), root / "repo", REPO_ID, sha)
            rerun = compile_commit(warm_store, root / "repo", REPO_ID, sha)
            shards = load_shards(warm_store, warm.manifest)
            out.append({
                "sha": sha, "warm": warm, "cold": cold, "rerun": rerun,
                "entities": {e["id"]: e for s in shards.values() for e in s["entities"]},
                "assertions": [a for s in shards.values() for a in s["assertions"]],
                "coverage": warm_store.get_json(warm.manifest.coverage_ref),
            })
        return out
    return run


def test_there_are_ten_fixtures():
    assert len(FIXTURES) == 10 and [f[:3] for f in FIXTURES] == [f"F{i:02d}" for i in range(1, 11)]


@pytest.mark.parametrize("name", FIXTURES)
def test_golden_is_well_formed(name):
    g = load_golden(name)
    assert g["fixture"] == name
    commit_dirs = sorted(p.name for p in (FIXTURES_DIR / name / "commits").iterdir() if p.is_dir())
    assert len(commit_dirs) >= 3 and [c["dir"] for c in g["commits"]] == commit_dirs
    items = g["entities"] + [e for c in g["commits"] for e in c["added_entities"] + c["removed_entities"]]
    assert g["entities"], "a fixture needs base expectations"
    for item in items:
        EntityKind(item["kind"])
        assert item["since"] in MILESTONES
    for item in g["absent_entities"]:
        EntityKind(item["kind"])
        assert item["why"]
    for edge in g["edges"]:
        assert edge["since"] in MILESTONES
        assert check_relation(edge["predicate"], EntityKind(edge["subject"]["kind"]), EntityKind(edge["object"]["kind"])) is None
    for kind in g["forbidden_kinds"]:
        EntityKind(kind)
    ids = [_eid(i) for i in g["entities"]]
    assert len(ids) == len(set(ids)), "duplicate golden entity"


@pytest.mark.parametrize("name", FIXTURES)
def test_base_commit_meets_m0_goldens(name, compiled):
    g, base = load_golden(name), compiled(name)[0]
    for item in (e for e in g["entities"] if e["since"] == "M0"):
        assert _eid(item) in base["entities"], f"missing {item}"
    for edge in (e for e in g["edges"] if e["since"] == "M0"):
        s, o = _eid(edge["subject"]), _eid(edge["object"])
        assert any(a["subject"] == s and a["object"] == o and a["predicate"] == edge["predicate"] for a in base["assertions"]), f"missing {edge}"
    for item in g["absent_entities"]:
        assert _eid(item) not in base["entities"], item["why"]


@pytest.mark.parametrize("name", FIXTURES)
def test_exclusions_and_unparsed_files_are_reported(name, compiled):
    g, base = load_golden(name), compiled(name)[0]
    inventory = next(c for c in base["coverage"] if c["domain"] == "inventory")
    want: dict[str, int] = {}
    for x in g["exclusions"]:
        want[x["reason"]] = want.get(x["reason"], 0) + 1
    for reason, n in want.items():
        assert inventory["detail"]["excluded_by_reason"].get(reason, 0) >= n, f"{reason} exclusions not reported"
    assert inventory["denominator"] - inventory["numerator"] == sum(inventory["detail"]["excluded_by_reason"].values())
    assert set(g.get("oversize_not_parsed", [])) <= set(inventory["detail"]["oversize_not_parsed"])
    for x in g["exclusions"]:
        assert entity_id(REPO_ID, "Artifact", {"path": x["path"]}) not in base["entities"]
    for path in g.get("oversize_not_parsed", []):  # inventoried, not parsed
        assert base["entities"][entity_id(REPO_ID, "Artifact", {"path": path})]["attributes"]["parsed"] is False


@pytest.mark.parametrize("name", FIXTURES)
def test_forbidden_kinds_never_appear(name, compiled):
    forbidden = set(load_golden(name)["forbidden_kinds"])
    for commit in compiled(name):
        assert not {e["kind"] for e in commit["entities"].values()} & forbidden


@pytest.mark.parametrize("name", FIXTURES)
def test_seeded_commits_add_and_remove_what_goldens_say(name, compiled):
    g, commits = load_golden(name), compiled(name)
    for i, spec in enumerate(g["commits"], start=1):
        before, after = commits[i - 1]["entities"], commits[i]["entities"]
        for item in (e for e in spec["added_entities"] if e["since"] == "M0"):
            assert _eid(item) in after and _eid(item) not in before, f"{spec['dir']}: {item} should be added"
        for item in (e for e in spec["removed_entities"] if e["since"] == "M0"):
            assert _eid(item) in before and _eid(item) not in after, f"{spec['dir']}: {item} should be removed"


@pytest.mark.parametrize("name", FIXTURES)
def test_replay_parity_and_reuse_on_every_commit(name, compiled):
    commits = compiled(name)
    for i, c in enumerate(commits):
        assert c["warm"].manifest.status == "complete"
        assert c["warm"].manifest.content_hash == c["cold"].manifest.content_hash, f"commit {i}: incremental differs from cold"
        assert c["warm"].manifest.rkp_hash == c["cold"].manifest.rkp_hash
        assert c["rerun"].stats.total_executed == 0
        if i:
            assert c["warm"].stats.total_executed < c["cold"].stats.total_executed
    assert len({c["warm"].manifest.rkp_hash for c in commits}) == len(commits), "every seeded commit should change the snapshot"


def test_adversarial_file_name_is_inert_data(compiled):
    base = compiled("F10-ambiguous-incomplete")[0]
    hostile = [e for e in base["entities"].values() if "IGNORE PREVIOUS INSTRUCTIONS" in e["name"]]
    assert len(hostile) == 1 and hostile[0]["kind"] == "Artifact"
    assert all(a["epistemic_status"] != "verified" for a in base["assertions"])


@pytest.mark.parametrize("name", FIXTURES)
def test_goldens_due_by_the_compiler_milestone(name, compiled):
    """Everything the goldens expect by the compiler's current milestone, on every commit."""
    from repo_intel.compile.golden_check import COMPILER_MILESTONE, check
    assert check(name, compiled(name), COMPILER_MILESTONE) == []


def test_dependency_capability_coverage_on_fixture_bases(compiled):
    all_declared, all_classified = set(), set()
    for name in FIXTURES:
        base = compiled(name)[0]
        declared = {
            e["id"]: (e["natural_key"]["ecosystem"], e["name"])
            for e in base["entities"].values()
            if e["kind"] == "ExternalDependency" and e["natural_key"]["ecosystem"] in ("npm", "pypi")
        }
        classified = {e["subject"] for e in base["assertions"] if e["predicate"] == "PROVIDES"}
        coverage = next(c for c in base["coverage"] if c["domain"] == "dependency_capability")
        assert (coverage["numerator"], coverage["denominator"]) == (len(classified), len(declared))
        all_declared.update(declared.values())
        all_classified.update(declared[subject] for subject in classified)
    assert (len(all_classified), len(all_declared)) == (15, 17)


def test_configuration_values_never_enter_the_graph(compiled):
    from repo_intel.storage.hashing import canonical
    for commit in compiled("F06-iac-heavy"):
        blob = canonical([commit["entities"], commit["assertions"]])
        assert b"postgresql://db/status" not in blob and b"status-db" not in blob.replace(b'"name":"status"', b"")
        assert b"DATABASE_URL" in blob  # the key name is kept, the value is not
