"""Architecture patterns and antipatterns (spec §8.8): the catalogue, the detectors and DM11."""

import json
from collections import Counter
from pathlib import Path

import pytest

from conftest import REPO_ID
from repo_intel.compile.fixtures import materialize
from repo_intel.compile.patterns import DETECTORS, Graph, PatternCatalogue, detect, granularity_features, load_patterns, parse_taxonomy
from repo_intel.compile.pipeline import compile_commit, load_shards
from repo_intel.decisions.architecture import LABEL_TO_ENTRY, granularity_rules, verify_granularity
from repo_intel.storage.store import ObjectStore
from test_decision_runtime import _repo

TAXONOMY = Path(__file__).resolve().parents[2] / "docs" / "architecture-pattern-taxonomy.md"


# ------------------------------------------------------------------ the catalogue


def test_catalogue_is_the_taxonomy_document():
    doc = parse_taxonomy(TAXONOMY.read_text())
    cat = load_patterns()
    assert [c.model_dump() for c in cat.concerns] == doc["concerns"]
    assert [(f.id, f.name, f.summary) for f in cat.families] == [(f["id"], f["name"], f["summary"]) for f in doc["families"]]
    assert [(e.id, e.kind, e.evolving, e.name, e.intent, e.family) for e in cat.entries] == [(e["id"], e["kind"], e["evolving"], e["name"], e["intent"], e["family"]) for e in doc["entries"]]
    assert [(a.name, a.typical_evidence, a.remediation) for a in cat.antipatterns] == [(a["name"], a["typical_evidence"], a["remediation"]) for a in doc["antipatterns"]]


def test_catalogue_counts_match_what_the_document_states():
    cat = load_patterns()
    assert (len(cat.concerns), len(cat.families), len(cat.entries), len(cat.antipatterns)) == (18, 94, 390, 9)
    assert Counter(e.kind for e in cat.entries) == {"style": 33, "architectural_pattern": 233, "design_pattern": 18, "technique": 75, "model": 16, "reference_architecture": 15}
    assert [e.id for e in cat.entries] == [f"arch-{i:04d}" for i in range(1, 391)], "ids are the document's, in its order"


def test_detectors_and_catalogue_reference_each_other():
    cat = load_patterns()
    referenced = {d for e in (*cat.entries, *cat.antipatterns) for d in e.detectors}
    assert referenced == set(DETECTORS), "every detector is in the catalogue, and the catalogue names no missing detector"
    assert {e.id for e in cat.entries if e.decided_by} == set(LABEL_TO_ENTRY.values())
    assert all(e.observability == "repository" for e in cat.entries if e.detectors or e.decided_by)
    beyond = {f.id for f in cat.families if f.observability == "beyond_repository"}
    assert beyond and all(f.observability_reason for f in cat.families if f.id in beyond)


def test_a_detector_on_an_entry_a_repository_cannot_show_is_rejected():
    data = json.loads(load_patterns().model_dump_json())
    beyond = next(e for e in data["entries"] if e["observability"] == "beyond_repository")
    beyond["detectors"] = ["data-ownership"]
    with pytest.raises(ValueError, match="cannot show"):
        PatternCatalogue.model_validate(data)


# ------------------------------------------------------------------ detectors on a constructed graph


def _ent(kind, id, natural_key, **attributes):
    return {"id": id, "kind": kind, "natural_key": natural_key, "name": natural_key.get("name") or natural_key.get("root") or id, "attributes": attributes, "evidence_ids": [f"ev_{id}"]}


def _rel(subject, predicate, obj):
    return {"id": f"a_{subject}_{predicate}_{obj}", "subject": subject, "predicate": predicate, "object": obj, "evidence_ids": [f"ev_{subject}_{obj}"]}


def _shared_database(outbox=False):
    entities = [
        _ent("Repository", "repo", {}),
        _ent("Component", "api", {"root": "services/api"}, component_kind="service"),
        _ent("Component", "jobs", {"root": "services/jobs"}, component_kind="worker"),
        _ent("Table", "orders", {"name": "orders"}),
        _ent("EventTopic", "created", {"name": "orders.created"}, transport="sqs"),
    ]
    assertions = [_rel("api", "WRITES_TO", "orders"), _rel("jobs", "WRITES_TO", "orders"), _rel("api", "PUBLISHES_TO", "created"), _rel("jobs", "SUBSCRIBES_TO", "created")]
    if outbox:
        entities.append(_ent("Table", "outbox", {"name": "orders_outbox"}))
        assertions.append(_rel("api", "WRITES_TO", "outbox"))
    return {"entities": entities, "assertions": assertions, "evidence": []}


def test_two_deployables_writing_one_table_share_a_database_and_show_the_antipattern():
    found = {(f.target, f.subject, f.evidence_class) for f in detect(Graph.build([_shared_database()]), load_patterns())}
    assert ("arch-0139", "orders", "E3") in found, "two components use the table: a parsed fact, so supported"
    assert ("anti-0004", "orders", "E4") in found, "'uncontrolled' is a judgement, so inferred"
    assert ("arch-0140", "repo", "E4") not in found, "a shared table rules out database per service"
    assert ("anti-0006", "api", "E4") in found, "writes a table and publishes, with no outbox"
    assert ("arch-0236", "created", "E4") in found and ("arch-0085", "created", "E3") in found


def test_an_outbox_table_answers_the_dual_write_gap():
    found = {(f.target, f.subject) for f in detect(Graph.build([_shared_database(outbox=True)]), load_patterns())}
    assert ("arch-0110", "outbox") in found and ("anti-0006", "api") not in found


def test_findings_cite_the_facts_they_rest_on():
    for f in detect(Graph.build([_shared_database()]), load_patterns()):
        assert f.facts and f.detector in DETECTORS and f.basis


# ------------------------------------------------------------------ DM11 deployment granularity


def _features(backend=(), frontend=(), functions=(), libraries=0, shared=(), links=0):
    return {"backend_deployables": list(backend), "frontend_deployables": list(frontend), "unlinked_functions": list(functions), "libraries": libraries, "shared_written_tables": list(shared), "cross_component_links": links}


@pytest.mark.parametrize("features, outcome, label", [
    (_features(), "insufficient_evidence", None),
    (_features(frontend=["web"]), "insufficient_evidence", None),
    (_features(backend=["api"], frontend=["web"]), "resolved", "monolithic"),
    (_features(backend=["api"], libraries=3), "ambiguous", None),
    (_features(backend=["api", "jobs"], shared=["orders"]), "resolved", "service-based"),
    (_features(backend=["a", "b", "c"], links=2), "resolved", "microservices"),
    (_features(backend=["a", "b", "c"]), "ambiguous", None),
    (_features(backend=["api"], functions=["ProcessOrders"]), "ambiguous", None),
])
def test_granularity_rules(features, outcome, label):
    r = granularity_rules(features)
    assert (r.outcome, r.label) == (outcome, label)
    if label:
        assert verify_granularity(features, label) != "rejected"


def test_granularity_verifier_rejects_labels_the_facts_contradict():
    assert verify_granularity(_features(backend=["a", "b"]), "monolithic") == "rejected"
    assert verify_granularity(_features(backend=["a"]), "microservices") == "rejected"
    assert verify_granularity(_features(backend=["a", "b"], shared=["orders"]), "microservices") == "rejected"


def test_granularity_features_read_the_shards():
    features, evidence = granularity_features({"u": _shared_database()}, [], 1)
    assert features == _features(backend=["services/api", "services/jobs"], shared=["orders"], links=1)
    assert evidence == ["ev_api", "ev_jobs"]


# ------------------------------------------------------------------ compiled


@pytest.fixture(scope="module")
def polyglot(tmp_path_factory):
    root = tmp_path_factory.mktemp("polyglot")
    store = ObjectStore(root / "store", "t")
    result = compile_commit(store, root / "repo", REPO_ID, materialize("F16-polyglot-contracts", root / "repo")[0])
    shards = load_shards(store, result.manifest)
    ledger = {e["decision_id"] for e in store.get_json(result.manifest.ledger_entries_ref)}
    return shards, store.get_json(result.manifest.coverage_ref), ledger


def test_status_follows_the_evidence_class(polyglot):
    shards, _, ledger = polyglot
    exhibits = [a for s in shards.values() for a in s["assertions"] if a["predicate"] == "EXHIBITS"]
    assert exhibits
    for a in exhibits:
        assert a["evidence_ids"], "every finding cites evidence"
        if a["evidence_class"] == "E3":
            assert (a["epistemic_status"], a["resolution"]) == ("supported", "exact")
        else:
            assert (a["epistemic_status"], a["resolution"]) == ("inferred", "candidate")
    decided = [a for a in exhibits if a["decision_ref"]]
    assert len(decided) == 1 and decided[0]["decision_ref"] in ledger and decided[0]["qualifiers"] == {"decision": "dm.deployment-granularity"}


def test_coverage_says_how_much_of_the_catalogue_was_looked_for(polyglot):
    _, coverage, _ = polyglot
    row = next(c for c in coverage if c["domain"] == "architecture_patterns")
    cat = load_patterns()
    assert row["numerator"] == sum(1 for e in cat.entries if e.detectors or e.decided_by)
    assert row["denominator"] == sum(1 for e in cat.entries if e.observability == "repository")
    assert row["detail"]["beyond_repository"] == 390 - row["denominator"]
    assert "arch-0004" in row["detail"]["exhibited"] and row["detail"]["antipatterns_exhibited"] == ["anti-0006"]


def test_a_dependency_alone_is_not_a_pattern(tmp_path):
    """The taxonomy's rule (§4): a framework name is not proof that a pattern is present."""
    repo = tmp_path / "repo"
    sha = _repo(repo, {"pyproject.toml": '[project]\nname = "deps-only"\nversion = "0.1.0"\ndependencies = ["kafka-python", "temporalio", "pybreaker", "redis", "boto3"]\n', "deps_only/__init__.py": ""})
    store = ObjectStore(tmp_path / "store", "t")
    shards = load_shards(store, compile_commit(store, repo, REPO_ID, sha).manifest)
    # DM11 still classifies the one component DM03 found (its kind may rest on dependencies); detectors add nothing.
    assert not [a for s in shards.values() for a in s["assertions"] if a["predicate"] == "EXHIBITS" and "detector" in a["qualifiers"]]


def test_an_undecided_granularity_is_an_unknown(tmp_path):
    root = tmp_path
    store = ObjectStore(root / "store", "t")
    result = compile_commit(store, root / "repo", REPO_ID, materialize("F02-ts-monorepo", root / "repo")[0])
    unknowns = store.get_json(result.manifest.unknowns_ref)
    assert [u["reason"] for u in unknowns if u["predicate"] == "deployment_granularity"] == ["ambiguous_no_model"]
