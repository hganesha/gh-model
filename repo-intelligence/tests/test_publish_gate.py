"""GI-017: a failing case for every publish-gate invariant (spec §14.1), conflicts and their
precedence, owner overrides end to end, and acceptance test 4 checked by type."""

import copy

import pytest
from fastapi.testclient import TestClient

from conftest import REPO_ID
from repo_intel.api import create_app
from repo_intel.compiler import preferred
from repo_intel.contracts import Assertion, Conflict, Evidence, Span, Unknown, assertion_id, entity_id
from repo_intel.decisions import overrides as override_store
from repo_intel.fixtures import materialize
from repo_intel.hashing import digest, merkle_root
from repo_intel.ontology import check_relation
from repo_intel.pipeline import EMPTY_OVERRIDES, compile_commit, load_shards, stage_snapshot
from repo_intel.store import ObjectStore
from repo_intel.verify import content_leaves, verify_snapshot

REQUIRED = ("inventory", "parse", "contract_match", "deployment_refs", "decisions")


@pytest.fixture
def snap(store, f01):
    repo, shas = f01
    m = compile_commit(store, repo, REPO_ID, shas[0]).manifest
    return {
        "pm": store.get_json(stage_snapshot(store, repo, shas[0])),
        "shards": load_shards(store, m),
        "coverage": store.get_json(m.coverage_ref),
        "conflicts": store.get_json(m.conflicts_ref),
    }


def gate(snap, *, shards=None, coverage=None, conflicts=None, rehash=True, **kw):
    """Run the gate. With `rehash`, digests and content_hash are recomputed from the rows given,
    so the only violation is the one the test planted."""
    shards = snap["shards"] if shards is None else shards
    coverage = snap["coverage"] if coverage is None else coverage
    conflicts = snap["conflicts"] if conflicts is None else conflicts
    source = shards if rehash else snap["shards"]
    digests = {n: digest(s) for n, s in source.items()}
    coverage_ref = digest(coverage if rehash else snap["coverage"])
    conflicts_ref = digest(conflicts if rehash else snap["conflicts"])
    content_hash = merkle_root(content_leaves(digests, coverage_ref, conflicts_ref))
    args = dict(repository_id=REPO_ID, path_manifest=snap["pm"], shards=shards, shard_digests=digests, coverage=coverage, coverage_ref=coverage_ref, conflicts=conflicts, conflicts_ref=conflicts_ref, content_hash=content_hash, required_coverage=REQUIRED)
    return verify_snapshot(**{**args, **kw})


def edit(snap):
    return copy.deepcopy(snap["shards"])


def first(shards, predicate=None, kind=None):
    for name, s in shards.items():
        for a in s["assertions"]:
            if (predicate is None or a["predicate"] == predicate) and (kind is None or a["epistemic_status"] == kind):
                return name, a
    raise LookupError(predicate)


def reid(a):
    a["id"] = assertion_id(a["subject"], a["predicate"], a["object"], a["qualifiers"])
    return a


def test_a_compiled_snapshot_passes(snap):
    assert gate(snap) == []


def test_relation_schema_violation_fails(snap):
    shards = edit(snap)
    _, a = first(shards, "CONTAINS")
    a["predicate"] = "DEPLOYED_TO"  # Repository/Component DEPLOYED_TO is not in the schema
    reid(a)
    assert any("DEPLOYED_TO: subject kind" in p for p in gate(snap, shards=shards))
    assert check_relation("DEPLOYED_TO", "Component", "Environment") is not None

    shards = edit(snap)
    name, a = first(shards, "CONTAINS", "supported")
    a.update(evidence_class="E5", epistemic_status="inferred")
    assert any("weaker than the minimum E3" in p for p in gate(snap, shards=shards))


def test_evidence_span_past_the_end_of_its_blob_fails(snap):
    shards = edit(snap)
    s = shards["."]
    ev = s["evidence"][0]
    size = next(e["size"] for e in snap["pm"]["entries"] if e["blob_sha256"] == ev["blob_sha256"])
    moved = Evidence.make(ev["blob_sha256"], Span(start_byte=0, end_byte=size + 10, start_line=1, end_line=1), ev["observation_kind"]).row()
    s["evidence"].append(moved)
    assert any(f"evidence {moved['id']}: span exceeds blob" in p for p in gate(snap, shards=shards))


def test_unknowns_need_a_reason_a_resolving_coverage_ref_and_subject(snap):
    shards = edit(snap)
    name = "."
    subject = shards[name]["entities"][0]["id"]
    u = Unknown.make(subject, "component_kind", "unknown", "planted", "decisions:.").row()
    shards[name]["unknowns"].insert(0, u)
    assert gate(snap, shards=shards) == [], "a well-formed unknown passes"
    nowhere = {**u, "coverage_ref": "nowhere:@repo"}
    shards[name]["unknowns"][0] = nowhere
    assert any("names no coverage entry" in p for p in gate(snap, shards=shards))

    blank = Unknown.make(u["subject"], u["predicate"], u["status"], " ", u["coverage_ref"]).row()
    shards[name]["unknowns"][0] = blank
    assert any("no reason" in p for p in gate(snap, shards=shards))

    orphan = Unknown.make("ent_" + "a" * 26, u["predicate"], u["status"], u["reason"], u["coverage_ref"]).row()
    shards[name]["unknowns"][0] = orphan
    assert any("subject ent_aaaa" in p and "does not resolve" in p for p in gate(snap, shards=shards))


def test_rows_are_rehashed_not_trusted_by_digest(snap):
    shards = edit(snap)
    e = shards["."]["entities"][0]
    e["name"] = e["name"] + "-edited"  # same digest recorded, different content
    problems = gate(snap, shards=shards, rehash=False)
    assert any("rows do not hash to the recorded digest" in p for p in problems)
    assert any("content_hash does not re-derive" in p for p in problems)

    edited = copy.deepcopy(snap["coverage"])
    edited[0]["numerator"] = 0
    assert any("coverage rows do not hash" in p for p in gate(snap, coverage=edited, rehash=False))
    assert any("conflict rows do not hash" in p for p in gate(snap, conflicts=[*snap["conflicts"], {"id": "cf_x"}], rehash=False))


def test_every_required_coverage_domain_is_reported_with_a_denominator(snap):
    without = [c for c in snap["coverage"] if c["domain"] != "decisions"]
    problems = gate(snap, coverage=without)
    assert any("decisions domain missing (required by the compile profile)" in p for p in problems)
    assert not any("domain missing" in p for p in gate(snap, coverage=without, required_coverage=("inventory",))), "the profile decides what is required"

    no_denominator = copy.deepcopy(snap["coverage"])
    no_denominator[0]["denominator"] = None
    assert any("no denominator" in p for p in gate(snap, coverage=no_denominator))


def test_hypotheses_never_alone_support_a_supported_assertion(snap):
    shards = edit(snap)
    name, a = first(shards, "CONTAINS", "supported")
    a["decision_ref"] = "dec_" + "h" * 26
    assert any("on hypothesis support alone" in p for p in gate(snap, shards=shards, hypothesis_decisions={a["decision_ref"]}))

    shards = edit(snap)
    name, a = first(shards, "CONTAINS", "supported")
    for ev in shards[name]["evidence"]:
        if ev["id"] in a["evidence_ids"]:
            ev["trust_label"] = "untrusted_generated"
    assert any(f"assertion {a['id']}: supported on hypothesis support alone" in p for p in gate(snap, shards=shards))

    with pytest.raises(ValueError, match="at most 'inferred'"):
        Assertion.make(a["subject"], "CONTAINS", a["object"], assertion_kind="hypothesis", epistemic_status="supported", evidence_class="E3", evidence_ids=a["evidence_ids"])


def _alias_pair(shards):
    s = shards["."]
    ents = [e["id"] for e in s["entities"] if e["kind"] == "Artifact"][:3]
    ev = s["evidence"][0]["id"]
    made = [Assertion.make(ents[0], "ALIAS_OF", obj, assertion_kind="inferred_relation", epistemic_status="inferred", evidence_class=cls, evidence_ids=[ev]).row() for obj, cls in ((ents[1], "E4"), (ents[2], "E5"))]
    s["assertions"] += made
    return made


def test_functional_violation_must_be_recorded_as_a_conflict(snap):
    shards = edit(snap)
    pair = _alias_pair(shards)
    assert any("single-valued predicate and no conflict recorded" in p for p in gate(snap, shards=shards))
    choice, basis = preferred(pair)
    recorded = Conflict.make("functional_violation", [pair[0]["subject"]], pair[0]["evidence_ids"], basis, [a["id"] for a in pair]).model_copy(update={"preferred": choice}).row()
    assert gate(snap, shards=shards, conflicts=[*snap["conflicts"], recorded]) == []


def test_conflict_precedence_is_override_then_evidence_class():
    rows = [
        {"id": "as_model", "object": "ent_m", "evidence_class": "E5", "assertion_kind": "inferred_relation", "resolution": "candidate"},
        {"id": "as_parse", "object": "ent_p", "evidence_class": "E2", "assertion_kind": "observed_fact", "resolution": "exact"},
        {"id": "as_declared", "object": "ent_d", "evidence_class": "E1", "assertion_kind": "observed_fact", "resolution": "exact"},
        {"id": "as_heuristic", "object": "ent_h", "evidence_class": "E4", "assertion_kind": "derived_relation", "resolution": "candidate"},
    ]
    assert preferred(rows) == ("as_declared", "evidence_class:E1")
    assert preferred(rows[:1] + rows[3:]) == ("as_heuristic", "evidence_class:E4"), "a heuristic outranks a model"
    assert preferred(rows, override_object="ent_m") == ("as_model", "owner_override")


def test_compiler_records_functional_conflicts_with_a_preference(tmp_path, monkeypatch):
    """repo.compile finds the violation itself and the owner's preference decides it."""
    from repo_intel import buildgraph, compiler
    real = compiler._dependency_capabilities

    def with_aliases(repo, shards, g, catalogue):
        out = real(repo, shards, g, catalogue)
        deps = sorted(e for e in g.entities if g.entities[e]["kind"] in ("ExternalDependency", "TechnicalCapability"))
        caps = [e for e in deps if g.entities[e]["kind"] == "TechnicalCapability"][:2]
        evs = sorted(g.evidence)[:1]
        for cap, cls in zip(caps, ("E3", "E4")):
            g.assertion(Assertion.make(repo_id(repo), "ALIAS_OF", cap, assertion_kind="derived_relation", epistemic_status="supported" if cls == "E3" else "inferred", evidence_class=cls, evidence_ids=evs))
        return out

    def repo_id(repo):
        return entity_id(repo, "Repository", {})

    monkeypatch.setattr(compiler, "_dependency_capabilities", with_aliases)
    monkeypatch.setenv(buildgraph.EXECUTOR_ENV, "inprocess")  # the patch exists only in this process
    repo = tmp_path / "repo"
    shas = materialize("F01-python-api", repo)
    store = ObjectStore(tmp_path / "s", "t")
    m = compile_commit(store, repo, REPO_ID, shas[0], use_cache=False).manifest
    conflicts = [c for c in store.get_json(m.conflicts_ref) if c["conflict_type"] == "functional_violation"]
    assert len(conflicts) == 1 and "preferred by evidence_class:E3" in conflicts[0]["basis"]
    shards = load_shards(store, m)
    by_id = {a["id"]: a for s in shards.values() for a in s["assertions"]}
    assert by_id[conflicts[0]["preferred"]]["evidence_class"] == "E3"

    weaker = next(by_id[i] for i in conflicts[0]["assertion_ids"] if i != conflicts[0]["preferred"])
    doc = override_store.SaveOverrides(expected_version=0, preferences=[override_store.PreferenceOverride(predicate="ALIAS_OF", subject=weaker["subject"], object=weaker["object"])])
    override_store.save(store, REPO_ID, doc, "owner")
    m2 = compile_commit(store, repo, REPO_ID, shas[0], use_cache=False).manifest
    chosen = next(c for c in store.get_json(m2.conflicts_ref) if c["conflict_type"] == "functional_violation")
    assert chosen["preferred"] == weaker["id"] and "owner_override" in chosen["basis"]
    assert m2.override_set_hash != m.override_set_hash and m2.rkp_hash != m.rkp_hash


# ---------------------------------------------------------------- owner overrides end to end


def test_owner_override_through_the_api_drives_the_compile(tmp_path):
    store = ObjectStore(tmp_path / "s", "tenant-a")
    repo = tmp_path / "repo"
    shas = materialize("F01-python-api", repo)
    before = compile_commit(store, repo, REPO_ID, shas[0]).manifest
    assert before.override_set_hash == EMPTY_OVERRIDES

    package = entity_id(REPO_ID, "Package", {"ecosystem": "pypi", "name": "orders-api", "manifest_dir": ""})
    client = TestClient(create_app("postgresql://unused", "tenant-a", "tok", store=store))
    auth = {"Authorization": "Bearer tok"}
    url = f"/v1/repositories/{REPO_ID}/overrides"
    body = {"expected_version": 0, "decisions": [{"spec": "dm.component-kind", "scope_entity": package, "label": "worker", "reason": "runs the order queue"}]}
    assert client.put(url, json=body).status_code == 401, "admin only"
    saved = client.put(url, json=body, headers=auth)
    assert saved.status_code == 200 and saved.json()["version"] == 1
    assert client.put(url, json=body, headers=auth).status_code == 409, "a stale version is refused"
    assert client.put(url, json={**body, "expected_version": 1, "decisions": [{**body["decisions"][0], "scope_entity": "not-an-id"}]}, headers=auth).status_code == 422
    current = client.get(url, headers=auth).json()
    assert current["version"] == 1 and current["decisions"][0]["label"] == "worker" and current["history"][0]["author"] == "local-admin"

    after = compile_commit(store, repo, REPO_ID, shas[0]).manifest
    assert after.override_set_hash == current["override_set_hash"] != EMPTY_OVERRIDES
    component = next(e for s in load_shards(store, after).values() for e in s["entities"] if e["kind"] == "Component")
    assert (component["attributes"]["component_kind"], component["attributes"]["kind_status"]) == ("worker", "owner_override")
    disagreement = next(c for c in store.get_json(after.conflicts_ref) if c["conflict_type"] == "label_disagreement")
    assert disagreement["preferred"] == "owner_override:worker" and "rules:service" in disagreement["basis"]

    replay = compile_commit(store, repo, REPO_ID, shas[0]).manifest
    assert replay.rkp_hash == after.rkp_hash, "the same override set replays exactly"


# ---------------------------------------------------------------- acceptance test 4, by type

DEPLOYMENT_KINDS = {"DeploymentUnit", "Environment", "InfrastructureResource", "Pipeline"}


def test_deployment_is_declared_never_observed(tmp_path):
    repo = tmp_path / "repo"
    shas = materialize("F06-iac-heavy", repo)
    store = ObjectStore(tmp_path / "s", "t")
    m = compile_commit(store, repo, REPO_ID, shas[0]).manifest
    shards = load_shards(store, m)
    pm = store.get_json(store.index_get("path_manifests", m.source_tree_hash))
    blobs = {e["blob_sha256"] for e in pm["entries"]}
    ents = {e["id"]: e for s in shards.values() for e in s["entities"]}
    evidence = {e["id"]: e for s in shards.values() for e in s["evidence"]}
    deployment = [a for s in shards.values() for a in s["assertions"] if ents[a["subject"]]["kind"] in DEPLOYMENT_KINDS or ents[a["object"]]["kind"] in DEPLOYMENT_KINDS]
    assert deployment and any(a["predicate"] == "DEPLOYED_AS" for a in deployment)
    for a in deployment:
        assert a["epistemic_status"] != "verified" and a["verification_ref"] is None, "no check against a running system exists, so nothing is verified"
        assert all(evidence[e]["blob_sha256"] in blobs for e in a["evidence_ids"]), "every claim rests on a file in this commit: declared state"
        assert all(evidence[e]["trust_label"] == "untrusted_data" for e in a["evidence_ids"])
    for e in ents.values():
        if e["kind"] in DEPLOYMENT_KINDS:
            assert e["evidence_ids"] and all(evidence[i]["blob_sha256"] in blobs for i in e["evidence_ids"])
    deployed_as = [a for a in deployment if a["predicate"] == "DEPLOYED_AS"]
    assert all((a["epistemic_status"], a["resolution"]) == ("inferred", "candidate") for a in deployed_as), "a name match is a lead"


def test_toolchain_records_extractor_versions_and_the_step_executor(store, f01):
    from repo_intel.extractors import EXTRACTORS
    repo, shas = f01
    m = compile_commit(store, repo, REPO_ID, shas[0]).manifest
    toolchain = store.get_json(m.toolchain_hash)
    assert set(toolchain["extractors"]) == {e.id for e in EXTRACTORS}
    assert toolchain["step_executor"] in ("sandbox-exec", "bwrap", "inprocess")
