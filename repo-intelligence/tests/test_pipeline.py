import pytest

from conftest import REPO_ID
from repo_intel.contracts import assertion_id, entity_id
from repo_intel.fixtures import load_golden
from repo_intel.pipeline import PublishGateError, compile_commit, load_manifest, load_shards, stage_compile, stage_snapshot
from repo_intel.store import ObjectStore
from repo_intel.verify import verify_snapshot

GOLDEN = load_golden("F01-python-api")


def _graph(store, manifest):
    shards = load_shards(store, manifest)
    return ({e["id"]: e for s in shards.values() for e in s["entities"]}, {a["id"]: a for s in shards.values() for a in s["assertions"]}, {v["id"]: v for s in shards.values() for v in s["evidence"]})


def _eid(item):
    return entity_id(REPO_ID, item["kind"], item["natural_key"])


def test_f01_matches_m0_goldens(store, f01):
    repo, shas = f01
    result = compile_commit(store, repo, REPO_ID, shas[0])
    assert result.manifest.status == "complete"
    entities, assertions, _ = _graph(store, result.manifest)
    for item in (g for g in GOLDEN["entities"] if g["since"] == "M0"):
        assert _eid(item) in entities, f"missing {item}"
    for edge in (g for g in GOLDEN["edges"] if g["since"] == "M0"):
        s, o = _eid(edge["subject"]), _eid(edge["object"])
        assert any(a["subject"] == s and a["object"] == o and a["predicate"] == edge["predicate"] for a in assertions.values()), f"missing {edge}"
    for item in GOLDEN["absent_entities"]:
        assert _eid(item) not in entities, item["why"]


def test_exclusions_are_recorded_never_silent(store, f01):
    repo, shas = f01
    pm = store.get_json(stage_snapshot(store, repo, shas[0]))
    recorded = {(x["path"], x["reason"]) for x in pm["exclusions"]}
    assert {(x["path"], x["reason"]) for x in GOLDEN["exclusions"]} <= recorded
    link = next(x for x in pm["exclusions"] if x["path"] == "docs-outside")
    assert link["detail"]["escapes_root"] is True
    assert all(e["path"] != "docs-outside" for e in pm["entries"])
    with pytest.raises(ValueError):
        stage_snapshot(store, repo, "main")  # refs must be resolved to a full SHA first


def test_every_assertion_resolves_to_evidence_in_the_snapshot(store, f01):
    repo, shas = f01
    result = compile_commit(store, repo, REPO_ID, shas[0])
    _, assertions, evidence = _graph(store, result.manifest)
    pm = store.get_json(stage_snapshot(store, repo, shas[0]))
    blobs = {e["blob_sha256"] for e in pm["entries"]}
    assert assertions
    for a in assertions.values():
        assert a["evidence_ids"] and all(evidence[i]["blob_sha256"] in blobs for i in a["evidence_ids"])
    dep = next(a for a in assertions.values() if a["predicate"] == "DEPENDS_ON" and a["qualifiers"]["specifier"] == ">=0.115")
    ev = evidence[dep["evidence_ids"][0]]
    text = store.get_bytes("sha256:" + ev["blob_sha256"])[ev["span"]["start_byte"]:ev["span"]["end_byte"]]
    assert text == b"fastapi>=0.115"


def test_replay_reproduces_rkp_hash_in_a_fresh_store(tmp_path, f01):
    repo, shas = f01
    a = compile_commit(ObjectStore(tmp_path / "a", "t"), repo, REPO_ID, shas[0])
    b = compile_commit(ObjectStore(tmp_path / "b", "t"), repo, REPO_ID, shas[0])
    assert a.manifest.rkp_hash == b.manifest.rkp_hash
    assert a.manifest.row() == b.manifest.row()


def test_second_run_of_unchanged_commit_executes_zero_steps(store, f01):
    repo, shas = f01
    assert compile_commit(store, repo, REPO_ID, shas[0]).stats.total_executed > 0
    assert compile_commit(store, repo, REPO_ID, shas[0]).stats.total_executed == 0


def test_incremental_equals_cold_on_every_seeded_commit(tmp_path, f01):
    repo, shas = f01
    warm = ObjectStore(tmp_path / "warm", "t")
    previous = None
    for i, sha in enumerate(shas):
        inc = compile_commit(warm, repo, REPO_ID, sha)
        cold = compile_commit(ObjectStore(tmp_path / f"cold{i}", "t"), repo, REPO_ID, sha)
        assert inc.manifest.content_hash == cold.manifest.content_hash
        assert inc.manifest.rkp_hash == cold.manifest.rkp_hash
        if previous:
            assert inc.stats.total_executed < cold.stats.total_executed, "incremental compile should reuse work"
            assert load_manifest(warm, rkp_hash=previous.rkp_hash).row() == previous.row(), "earlier snapshot must be untouched"
        previous = inc.manifest


def test_seeded_commits_change_what_the_goldens_say(store, f01):
    repo, shas = f01
    base, add_col, add_dep, moved = (compile_commit(store, repo, REPO_ID, s) for s in shas)
    pyd = entity_id(REPO_ID, "ExternalDependency", {"ecosystem": "pypi", "name": "pydantic-settings"})
    deps = lambda m: {e for e, row in _graph(store, m.manifest)[0].items() if row["kind"] == "ExternalDependency"}
    assert deps(base) == deps(add_col) and pyd not in deps(add_col)
    assert add_col.stats.executed["file.extract"] == 1 and add_col.stats.executed.get("unit.plan", 0) == 0
    assert deps(add_dep) - deps(add_col) == {pyd}
    old, new = (entity_id(REPO_ID, "Artifact", {"path": p}) for p in ("app/models.py", "app/domain/models.py"))
    ents = _graph(store, moved.manifest)[0]
    assert old not in ents and new in ents


def test_publish_gate_rejects_broken_snapshots(store, f01):
    repo, shas = f01
    result = compile_commit(store, repo, REPO_ID, shas[0])
    m = result.manifest
    pm = store.get_json(stage_snapshot(store, repo, shas[0]))
    shards = load_shards(store, m)
    digests = {s.unit: s.digest for s in m.shards}
    coverage = store.get_json(m.coverage_ref)
    args = dict(repository_id=REPO_ID, path_manifest=pm, shard_digests=digests, coverage=coverage, coverage_ref=m.coverage_ref, conflicts_ref=m.conflicts_ref, content_hash=m.content_hash)
    assert verify_snapshot(shards=shards, **args) == []

    import copy
    dangling = copy.deepcopy(shards)
    a = dangling["."]["assertions"][0]
    a["object"] = "ent_doesnotexist"
    a["id"] = assertion_id(a["subject"], a["predicate"], a["object"], a["qualifiers"])
    assert any("does not resolve" in p for p in verify_snapshot(shards=dangling, **args))

    unpinned = copy.deepcopy(pm)
    unpinned["entries"] = [e for e in unpinned["entries"] if e["path"] != "pyproject.toml"]
    assert any("not in the snapshot manifest" in p for p in verify_snapshot(shards=shards, **{**args, "path_manifest": unpinned}))

    assert any("content_hash" in p for p in verify_snapshot(shards=shards, **{**args, "content_hash": "sha256:" + "0" * 64}))

    wrong_repo = verify_snapshot(shards=shards, **{**args, "repository_id": "github.com/other"})
    assert any("does not re-derive" in p for p in wrong_repo)
