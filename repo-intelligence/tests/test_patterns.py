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


# ------------------------------------------------------------------ structure and coupling (phase 2)


def _module_graph(files, imports, root="svc"):
    """A component whose package holds `files` (paths below `root`), importing as `imports` says."""
    entities = [_ent("Repository", "repo", {}), _ent("Component", "c", {"root": root}, component_kind="service"), _ent("Package", "p", {"name": "svc"})]
    assertions = [_rel("c", "CONTAINS", "p")]
    for f in files:
        entities.append(_ent("Artifact", f, {"path": f"{root}/{f}"}, language="python"))
        assertions.append(_rel("p", "CONTAINS", f))
    assertions += [_rel(a, "DEPENDS_ON", b) for a, b in imports]
    return {"entities": entities, "assertions": assertions, "evidence": []}


def _targets(shard):
    return {(f.target, f.subject) for f in detect(Graph.build([shard]), load_patterns())}


def test_layers_that_import_only_downwards_are_layered():
    files = ["app/api/routes.py", "app/services/orders.py", "app/repositories/orders.py"]
    down = [(files[0], files[1]), (files[1], files[2])]
    assert {("arch-0017", "c"), ("arch-0026", "c")} <= _targets(_module_graph(files, down))
    assert not {("arch-0017", "c"), ("arch-0026", "c")} & _targets(_module_graph(files, down + [(files[2], files[1])])), "an upward import breaks the layering"
    skipping = _targets(_module_graph(files, down + [(files[0], files[2])]))
    assert ("arch-0017", "c") in skipping and ("arch-0026", "c") not in skipping, "presentation reaching the data layer is still layered, but has no service layer"


def test_a_large_import_cycle_across_directories_is_a_big_ball_of_mud():
    files = [f"{d}/m{i}.py" for i, d in enumerate(["a", "b", "c", "d", "e"])]
    ring = [(files[i], files[(i + 1) % 5]) for i in range(5)]
    assert ("anti-0002", "c") in _targets(_module_graph(files, ring))
    assert ("anti-0002", "c") not in _targets(_module_graph(files, ring[:-1])), "no cycle, no tangle"


def test_a_module_most_files_import_and_that_imports_most_is_a_god_module():
    files = ["core.py", *(f"f{i}.py" for i in range(8))]
    hub = [(f, "core.py") for f in files[1:]] + [("core.py", f) for f in files[1:6]]
    assert ("anti-0003", "core.py") in _targets(_module_graph(files, hub))
    leaf = [(f, "core.py") for f in files[1:]]
    assert ("anti-0003", "core.py") not in _targets(_module_graph(files, leaf)), "a shared utility that imports nothing is not a god module"


def _services(calls, shared_table=False):
    """Components s0..sN, each exposing one endpoint e<i>; `calls` are (caller, callee) indexes."""
    n = 1 + max(max(c) for c in calls)
    entities = [_ent("Repository", "repo", {})]
    assertions = []
    for i in range(n):
        entities += [_ent("Component", f"s{i}", {"root": f"s{i}"}, component_kind="service"), _ent("Endpoint", f"e{i}", {"route": f"/s{i}", "method": "GET"})]
        assertions.append(_rel(f"s{i}", "EXPOSES", f"e{i}"))
    assertions += [_rel(f"s{a}", "CONSUMES", f"e{b}") for a, b in calls]
    if shared_table:
        entities.append(_ent("Table", "t", {"name": "orders"}))
        assertions += [_rel("s0", "WRITES_TO", "t"), _rel("s1", "WRITES_TO", "t")]
    return {"entities": entities, "assertions": assertions, "evidence": []}


def test_services_calling_each_other_both_ways_are_a_distributed_monolith():
    assert ("anti-0001", "repo") in _targets(_services([(0, 1), (1, 0)]))
    assert ("anti-0001", "repo") in _targets(_services([(0, 1)], shared_table=True)), "shared written tables and a synchronous call"
    assert ("anti-0001", "repo") not in _targets(_services([(0, 1)]))


def test_three_synchronous_hops_are_a_chatty_chain():
    assert ("anti-0005", "s0") in _targets(_services([(0, 1), (1, 2), (2, 3)]))
    assert not any(t == "anti-0005" for t, _ in _targets(_services([(0, 1), (1, 2)])))


def test_code_traits_cite_the_exact_spans():
    shard = _module_graph(["client.py"], [])
    shard["entities"][-1]["attributes"]["code_traits"] = [
        {"function": "reserve", "trait": "retry", "library": "tenacity", "backoff": False, "bounded": False, "evidence_id": "ev_retry"},
        {"function": "loop", "trait": "agent_loop", "bounded": True, "evidence_id": "ev_loop"},
    ]
    found = {f.target: f for f in detect(Graph.build([shard]), load_patterns()) if f.detector == "code-traits"}
    assert set(found) == {"anti-0007"}, "a bounded agent loop is not an antipattern"
    assert found["anti-0007"].subject == "c" and found["anti-0007"].evidence == ("ev_retry",)


RUNTIME = ("retry", "unbounded_fanout", "agent_loop")


def test_extractors_read_retry_fanout_and_agent_loop_traits():
    from repo_intel.extractors.python_source import PythonSource
    from repo_intel.extractors.typescript_source import TypeScriptSource
    py = b'''import asyncio
from tenacity import retry, wait_exponential

@retry
def fast(): ...

@retry(wait=wait_exponential())
def slow(): ...

async def fan(xs):
    await asyncio.gather(*[go(x) for x in xs])

async def fan_limited(xs):
    sem = asyncio.Semaphore(4)
    await asyncio.gather(*[go(x) for x in xs])

def agent(client, messages):
    while True:
        r = client.messages.create(model="m", messages=messages, tools=TOOLS)
        messages.append(run_tools(r))

def capped(client, messages):
    turns = 0
    while True:
        turns += 1
        if turns >= MAX_TURNS:
            break
        r = client.messages.create(model="m", messages=messages, tools=TOOLS)
'''
    traits = [(o.payload["function"], o.payload["trait"], o.payload.get("backoff", o.payload.get("bounded"))) for o in PythonSource().extract("a.py", py, "b").observations if o.kind == "code_trait" and o.payload["trait"] in RUNTIME]
    assert traits == [("fast", "retry", False), ("slow", "retry", True), ("fan", "unbounded_fanout", None), ("agent", "agent_loop", False), ("capped", "agent_loop", True)]
    ts = b'''import axiosRetry from "axios-retry";
axiosRetry(axios, { retries: 3 });
export async function all(xs: string[]) { await Promise.all(xs.map((x) => go(x))); }
export async function agent() {
  while (true) { const r = await client.chat.completions.create({ model: "m", messages, tools }); messages.push(r); }
}
'''
    traits = [(o.payload["trait"], o.payload.get("backoff", o.payload.get("bounded"))) for o in TypeScriptSource().extract("a.ts", ts, "b").observations if o.kind == "code_trait" and o.payload["trait"] in RUNTIME]
    assert traits == [("retry", False), ("unbounded_fanout", None), ("agent_loop", False)]


def _resilience_targets(traits_by_file):
    shard = _module_graph(list(traits_by_file), [])
    for e in shard["entities"]:
        if e["kind"] == "Artifact":
            e["attributes"]["code_traits"] = [{"function": "f", **t, "evidence_id": f"ev_{e['id']}_{i}"} for i, t in enumerate(traits_by_file[e["id"]])]
    return {f.target: f for f in detect(Graph.build([shard]), load_patterns()) if f.detector == "resilience"}


def test_resilience_policies_are_patterns_of_the_component():
    found = _resilience_targets({
        "gateway.py": [{"trait": "circuit_breaker", "library": "pybreaker"}, {"trait": "timeout", "construct": "httpx.post", "deadline": False}],
        "api.py": [{"trait": "rate_limit", "library": "slowapi", "direction": "inbound"}],
        "pools.py": [{"trait": "concurrency_limit", "name": "inventory"}, {"trait": "concurrency_limit", "name": "payments"}],
    })
    assert set(found) == {"arch-0212", "arch-0213", "arch-0214", "arch-0239"}
    assert all(f.subject == "c" and f.evidence_class == "E4" for f in found.values())
    assert found["arch-0213"].evidence == ("ev_gateway.py_0",), "a finding cites the construct's own span"
    assert "no deadline is seen carried on" in found["arch-0212"].basis
    assert "bounded pool" in found["arch-0214"].basis


def test_one_bounded_pool_is_not_a_bulkhead_and_a_propagated_deadline_is_said():
    found = _resilience_targets({"a.go": [{"trait": "concurrency_limit", "name": "sem"}, {"trait": "timeout", "construct": "context.WithTimeout", "deadline": True}]})
    assert "arch-0214" not in found, "one limit bounds work; it does not partition capacity"
    assert "a deadline is carried on to downstream calls" in found["arch-0212"].basis
    assert "arch-0214" in _resilience_targets({"a.ts": [{"trait": "bulkhead", "library": "cockatiel"}]}), "an explicit bulkhead is one"


def _traits(extractor, path, source):
    return [(o.payload["function"], o.payload["trait"]) for o in extractor.extract(path, source, "b").observations if o.kind == "code_trait" and o.payload["trait"] not in RUNTIME]


def test_extractors_read_resilience_policies_in_five_languages():
    from repo_intel.extractors.python_source import PythonSource
    from repo_intel.extractors.source_dotnet import DotnetSource
    from repo_intel.extractors.source_go import GoSource
    from repo_intel.extractors.source_jvm import JvmSource
    from repo_intel.extractors.typescript_source import TypeScriptSource
    assert _traits(PythonSource(), "a.py", b"""import httpx, pybreaker
from slowapi import Limiter
breaker = pybreaker.CircuitBreaker(fail_max=5)
limiter = Limiter(key_func=key)
def call():
    return httpx.get("http://x", timeout=2)
def untimed():
    return httpx.get("http://x")
""") == [("<module>", "circuit_breaker"), ("<module>", "rate_limit"), ("call", "timeout")]
    assert _traits(TypeScriptSource(), "a.ts", b"""import CircuitBreaker from "opossum";
import rateLimit from "express-rate-limit";
import { bulkhead } from "cockatiel";
import pLimit from "p-limit";
const b = new CircuitBreaker(call);
app.use(rateLimit({ max: 10 }));
const bh = bulkhead(4);
const limit = pLimit(2);
export async function call() { return fetch("http://x", { signal: AbortSignal.timeout(1000) }); }
""") == [("<module>", "circuit_breaker"), ("<module>", "rate_limit"), ("<module>", "bulkhead"), ("<module>", "concurrency_limit"), ("call", "timeout")]
    assert _traits(JvmSource(), "A.java", b"""import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
public class A {
  @CircuitBreaker(name = "inv")
  @Bulkhead(name = "inv")
  public String reserve(String sku) { return client.get(sku); }
  @RateLimiter(name = "inv")
  public void list() { stub.withDeadlineAfter(2, TimeUnit.SECONDS).list(); }
}
""") == [("reserve", "circuit_breaker"), ("reserve", "bulkhead"), ("list", "rate_limit"), ("list", "timeout")]
    assert _traits(DotnetSource(), "A.cs", b"""using Polly;
public class A {
  public void Configure(IServiceCollection s) {
    var cb = Policy.Handle<Exception>().CircuitBreakerAsync(5, TimeSpan.FromSeconds(30));
    var bh = Policy.BulkheadAsync(10);
    s.AddHttpClient("x").ConfigureHttpClient(c => c.Timeout = TimeSpan.FromSeconds(2));
  }
  [EnableRateLimiting("fixed")]
  public IActionResult Get() { return Ok(); }
}
""") == [("Configure", "circuit_breaker"), ("Configure", "bulkhead"), ("Get", "rate_limit"), ("Configure", "timeout")]
    assert _traits(GoSource(), "a.go", b"""package a
import ("context"; "github.com/sony/gobreaker"; "golang.org/x/time/rate")
var cb = gobreaker.NewCircuitBreaker(gobreaker.Settings{})
var lim = rate.NewLimiter(10, 5)
func call(ctx context.Context) {
	ctx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
}
""") == [("call", "timeout"), ("<module>", "circuit_breaker"), ("<module>", "rate_limit")]


def test_a_library_named_without_its_construct_is_not_a_policy():
    from repo_intel.extractors.python_source import PythonSource
    from repo_intel.extractors.typescript_source import TypeScriptSource
    assert _traits(PythonSource(), "a.py", b"import pybreaker\nimport slowapi\ndef f():\n    return 1\n") == []
    assert _traits(TypeScriptSource(), "a.ts", b'import CircuitBreaker from "x";\nconst b = new CircuitBreaker(f);\n') == [], "a CircuitBreaker class from an unknown library"


def test_import_edges_join_the_files_of_a_unit(polyglot):
    shards, _, _ = polyglot
    entities = {e["id"]: e for s in shards.values() for e in s["entities"]}
    imports = [a for s in shards.values() for a in s["assertions"] if a["predicate"] == "DEPENDS_ON" and entities.get(a["subject"], {}).get("kind") == "Artifact"]
    for a in imports:
        assert entities[a["object"]]["kind"] == "Artifact" and a["evidence_class"] == "E2" and a["qualifiers"] == {"via": "import"}
