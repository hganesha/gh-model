"""Decision runner, gateway adapters, and decisions inside the compile."""

import json

import httpx
import pytest

from conftest import REPO_ID
from repo_intel.contracts import DecisionSpec
from repo_intel.decisions import component as dm
from repo_intel.decisions.gateway import EgressDenied, Gateway, ModelUnavailable, ScriptedModel, jev, microsoft_decision
from repo_intel.decisions.runner import NONE_OF_THESE, Budget, Ledger, RuleResult, evaluate
from repo_intel.fixtures import materialize
from repo_intel.pipeline import compile_commit, load_shards
from repo_intel.store import ObjectStore

SPEC = dm.SPEC
FEATURES = {"ecosystem": "pypi", "package": "x", "http_endpoints": 0}
AMBIGUOUS = lambda f: RuleResult("ambiguous", candidates=("service", "library", "cli"))


def gateway_with(pick, allowed=("scripted",)):
    model = ScriptedModel(pick)
    return Gateway({"decision-default": model}, frozenset(allowed)), model


def run(store, rules=AMBIGUOUS, features=FEATURES, **kw):
    return evaluate(SPEC, "ent_scope", features, rules, ledger=Ledger(store), criteria=dm.CRITERIA, **kw)


# ---------------------------------------------------------------- the gate


def test_resolved_rules_never_call_a_model(store):
    gw, model = gateway_with(lambda s, o: "library")
    r = run(store, lambda f: RuleResult("resolved", "service"), gateway=gw)
    assert (r.selected_label, r.decision_method, r.abstained, model.calls) == ("service", "rules", False, [])


def test_missing_evidence_abstains_without_calling_a_model(store):
    gw, model = gateway_with(lambda s, o: "library")
    r = run(store, lambda f: RuleResult("insufficient_evidence", reason="language_inventory_only"), gateway=gw)
    assert r.abstained and r.abstain_reason == "language_inventory_only" and model.calls == []


def test_ambiguous_without_a_model_abstains(store):
    r = run(store)
    assert r.abstained and r.abstain_reason == "ambiguous_no_model" and r.alternatives == ["cli", "library", "service"]


def test_label_outside_the_label_set_is_rejected(store):
    with pytest.raises(ValueError):
        run(store, lambda f: RuleResult("resolved", "microservice"))


# ---------------------------------------------------------------- decision model


def test_decision_model_resolves_an_ambiguous_case(store):
    gw, model = gateway_with(lambda s, o: "library")
    r = run(store, gateway=gw)
    assert (r.selected_label, r.decision_method, r.rule_outcome, r.model_ref) == ("library", "decision_model", "ambiguous", "scripted/scripted-1")
    assert set(r.option_scores) == {"cli", "library", "service", NONE_OF_THESE} and r.confidence is None, "raw scores are kept; no calibrated confidence is claimed"
    assert len(model.calls) == 3 and r.attempts == 3, "one canonical call plus two permutations"
    assert model.calls[0] == ["cli", "library", "service", NONE_OF_THESE], "canonical order, with an explicit way to decline"
    assert all(sorted(c) == sorted(model.calls[0]) for c in model.calls) and any(c != model.calls[0] for c in model.calls[1:])


def test_only_rule_candidates_are_offered(store):
    gw, model = gateway_with(lambda s, o: "service")
    run(store, lambda f: RuleResult("ambiguous", candidates=("service", "worker", "not-a-label")), gateway=gw)
    assert model.calls[0] == ["service", "worker", NONE_OF_THESE]


def test_order_sensitive_answers_abstain(store):
    gw, _ = gateway_with(lambda s, options: options[0])  # always picks whatever is listed first
    r = run(store, gateway=gw)
    assert r.abstained and r.abstain_reason == "order_sensitive" and r.option_scores


@pytest.mark.parametrize("scores,reason", [
    ({"library": 0.6, "service": 0.3, "cli": 0.1}, "below_threshold"),
    ({"library": 0.8, "service": 0.2}, None),
    ({NONE_OF_THESE: 0.9, "library": 0.1}, NONE_OF_THESE),
])
def test_threshold_and_decline(store, scores, reason):
    gw, _ = gateway_with(lambda s, o: scores)
    r = run(store, gateway=gw)
    assert r.abstain_reason == reason and r.abstained == (reason is not None)


def test_margin_is_enforced(store):
    spec = DecisionSpec.model_validate({**SPEC.row(), "strategy": [{"kind": "rules"}, {"kind": "decision_model", "when": "ambiguous", "model_ref": "decision-default", "accept_threshold": 0.4, "min_margin": 0.25}]})
    gw, _ = gateway_with(lambda s, o: {"library": 0.5, "service": 0.45, "cli": 0.05})
    r = evaluate(spec, "ent_scope", FEATURES, AMBIGUOUS, ledger=Ledger(store), gateway=gw)
    assert r.abstained and r.abstain_reason == "below_margin"


def test_ledger_reuses_a_decision_and_a_changed_input_is_a_new_one(store):
    gw, model = gateway_with(lambda s, o: "library")
    first = run(store, gateway=gw, evidence_ids=("ev_a",))
    again = run(store, gateway=gw, evidence_ids=("ev_a",))
    assert again == first and len(model.calls) == 3, "second evaluation is a ledger hit: no model call"
    moved = run(store, gateway=gw, evidence_ids=("ev_b",))
    assert moved.decision_id != first.decision_id and moved.evidence_ids == ["ev_b"], "moved evidence must not reuse stale references"
    assert run(store, gateway=gw, features={**FEATURES, "http_endpoints": 2}, evidence_ids=("ev_a",)).decision_id != first.decision_id


def test_budget_outage_and_egress_abstain_without_poisoning_the_ledger(store):
    gw, model = gateway_with(lambda s, o: "library")
    r = run(store, gateway=gw, budget=Budget(max_calls=2))
    assert r.abstain_reason == "budget_exhausted" and model.calls == []
    denied, blocked = gateway_with(lambda s, o: "library", allowed=())
    assert run(store, gateway=denied).abstain_reason == "egress_denied" and blocked.calls == [], "egress is denied unless policy names the provider"

    def down(state, options):
        raise ModelUnavailable("down")
    assert run(store, gateway=gateway_with(down)[0]).abstain_reason == "model_unavailable"
    assert run(store, gateway=Gateway()).abstain_reason == "model_unavailable"  # alias not configured
    recovered = run(store, gateway=gw)
    assert recovered.selected_label == "library", "a transient failure is retried on the next compile, not frozen"


def test_model_sees_features_as_labelled_data_only(store):
    seen = []
    gw, _ = gateway_with(lambda state, o: seen.append(state) or "library")
    run(store, gateway=gw, evidence_ids=("ev_a",))
    assert seen[0] == {"trust": "untrusted_data", "scope": "unit", "features": FEATURES}


# ---------------------------------------------------------------- provider adapters (request shape per vendor docs; not run live)


def _transport(captured, answer=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        options = list(json.loads(request.content)["questions"]["decision"]["criteria"])
        body = answer if answer is not None else {"model": "jev-1.13.0", "answers": {"decision": {"type": "choice", "choice": options[0], "confidence": 0.9, "probabilities": {o: (0.9 if i == 0 else 0.1 / (len(options) - 1)) for i, o in enumerate(options)}}}, "usage": {"input_tokens": 120, "output_tokens": 3}}
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


def test_jev_request_and_response_shape():
    sent = []
    a = jev("k-test", transport=_transport(sent)).choose({"x": 1}, "Which?", {"service": "serves", "library": "imported"})
    req = sent[0]
    assert str(req.url) == "https://api.typesafe.ai/v1/systemone" and req.headers["authorization"] == "Bearer k-test"
    assert json.loads(req.content) == {"model": "jev-latest", "state": {"x": 1}, "questions": {"decision": {"type": "choice", "instructions": "Which?", "criteria": {"service": "serves", "library": "imported"}}}}
    assert (a.choice, a.model, a.input_tokens) == ("service", "jev-1.13.0", 120) and set(a.probabilities) == {"service", "library"}


def test_microsoft_decision_request_shape_and_auth():
    sent = []
    microsoft_decision("https://res.example.azure.com/", "my-deployment", api_key="k-test", transport=_transport(sent)).choose("s", "Which?", {"a": "A", "b": "B"})
    req = sent[0]
    assert str(req.url) == "https://res.example.azure.com/providers/microsoft/v1/systemone"
    assert req.headers["api-key"] == "k-test" and "authorization" not in req.headers and json.loads(req.content)["model"] == "my-deployment"
    sent.clear()
    microsoft_decision("https://res.example.azure.com", "d", entra_token="t-test", transport=_transport(sent)).choose("s", "Which?", {"a": "A", "b": "B"})
    assert sent[0].headers["authorization"] == "Bearer t-test" and "api-key" not in sent[0].headers
    with pytest.raises(ValueError):
        microsoft_decision("https://x", "d")

    class FakeCredential:
        asked = []
        def get_token(self, scope):
            self.asked.append(scope)
            return type("T", (), {"token": f"tok-{len(self.asked)}"})()
    sent.clear()
    client = microsoft_decision("https://res.example.azure.com", "d", credential=FakeCredential(), transport=_transport(sent))
    client.choose("s", "Which?", {"a": "A", "b": "B"})
    client.choose("s", "Which?", {"a": "A", "b": "B"})
    assert [r.headers["authorization"] for r in sent] == ["Bearer tok-1", "Bearer tok-2"], "a fresh token per call"
    assert FakeCredential.asked == ["https://cognitiveservices.azure.com/.default"] * 2


@pytest.mark.parametrize("answer,status", [
    (None, 429), (None, 500), ({"answers": {}}, 200),
    ({"model": "m", "answers": {"decision": {"type": "choice", "choice": "zzz", "probabilities": {"a": 1, "b": 0}}}}, 200),
    ({"model": "m", "answers": {"decision": {"type": "choice", "choice": "a"}}}, 200),  # no per-option probabilities
])
def test_unusable_provider_answers_are_rejected(answer, status):
    with pytest.raises(ModelUnavailable):
        jev("k", transport=_transport([], answer, status)).choose("s", "Which?", {"a": "A", "b": "B"})


def test_gateway_from_env_and_default_deny():
    gw = Gateway.from_env(env={"RKP_JEV_API_KEY": "k", "RKP_MSDECISION_ENDPOINT": "https://r", "RKP_MSDECISION_DEPLOYMENT": "d", "RKP_MSDECISION_API_KEY": "k2", "RKP_DECISION_DEFAULT": "jev"})
    assert gw.describe() == {"decision-default": "jev/jev-latest", "jev": "jev/jev-latest", "microsoft-decision": "microsoft-decision/d"}
    assert "k2" not in repr(gw.describe())
    with pytest.raises(EgressDenied):
        gw.resolve("jev")
    assert Gateway.from_env(frozenset({"jev"}), env={"RKP_JEV_API_KEY": "k"}).resolve("jev").provider == "jev"
    assert Gateway.from_env(env={}).aliases == {}


# ---------------------------------------------------------------- decisions inside the compile


def _compile(tmp_path, fixture, commit=0, gateway=None, name="s"):
    repo = tmp_path / f"repo-{fixture}"
    shas = materialize(fixture, repo) if not repo.exists() else None
    shas = shas or __import__("subprocess").run(["git", "-C", str(repo), "rev-list", "--reverse", "HEAD"], capture_output=True, text=True, check=True).stdout.split()
    store = ObjectStore(tmp_path / name, "t")
    result = compile_commit(store, repo, REPO_ID, shas[commit], gateway=gateway)
    shards = load_shards(store, result.manifest)
    ents = {e["id"]: e for s in shards.values() for e in s["entities"]}
    return {
        "result": result, "store": store, "entities": ents,
        "by_kind": lambda k: [e for e in ents.values() if e["kind"] == k],
        "edges": {(ents[a["subject"]]["kind"], ents[a["subject"]]["name"], a["predicate"], ents[a["object"]]["name"]): a for s in shards.values() for a in s["assertions"]},
        "unknowns": [(ents[u["subject"]]["kind"], u["predicate"], u["reason"]) for u in store.get_json(result.manifest.unknowns_ref)],
    }


@pytest.mark.parametrize("fixture,topology", [("F01-python-api", "single"), ("F02-ts-monorepo", "monorepo"), ("F03-python-ts-hybrid", "hybrid"), ("F04-java-maven", "multi-package"), ("F10-ambiguous-incomplete", "unknown")])
def test_repository_topology(tmp_path, fixture, topology):
    g = _compile(tmp_path, fixture)
    assert g["by_kind"]("Repository")[0]["attributes"]["topology"] == topology
    assert (("Repository", "topology", "no_package_manifest") in g["unknowns"]) == (topology == "unknown")


def test_component_kinds_by_rules_in_a_monorepo(tmp_path):
    g = _compile(tmp_path, "F02-ts-monorepo")
    kinds = {e["natural_key"]["root"]: (e["attributes"]["component_kind"], e["attributes"]["kind_status"]) for e in g["by_kind"]("Component")}
    assert kinds == {"": ("tooling", "supported"), "apps/api": ("service", "supported"), "apps/web": ("frontend_app", "supported"), "apps/worker": ("worker", "supported"), "packages/shared": ("library", "supported"), "packages/ui": ("library", "supported")}
    assert g["edges"][("Component", "@acme/api", "EXPOSES", "GET /orders")]["epistemic_status"] == "supported"
    owner = g["edges"][("Component", "@acme/api", "WRITES_TO", "orders")]
    assert (owner["resolution"], owner["epistemic_status"], owner["qualifiers"]) == ("candidate", "inferred", {"basis": "schema_owner"})
    assert g["edges"][("Component", "@acme/web", "CONSUMES", "GET /orders")]["resolution"] == "candidate"


def test_component_data_access_lifts_exact_symbol_links(tmp_path):
    g = _compile(tmp_path, "F01-python-api")
    a = g["edges"][("Component", "orders-api", "WRITES_TO", "customers")]
    assert (a["resolution"], a["evidence_class"], a["qualifiers"]) == ("exact", "E3", {"via": "symbol"}) and a["decision_ref"]


def test_unsupported_language_yields_unknown_not_component(tmp_path):
    g = _compile(tmp_path, "F04-java-maven")
    assert g["by_kind"]("Component") == []
    assert sum(1 for u in g["unknowns"] if u[:2] == ("Package", "component_boundary")) == 3


def test_ambiguous_component_abstains_without_a_model_and_resolves_with_one(tmp_path):
    plain = _compile(tmp_path, "F08-ai-agent", name="plain")
    comp = plain["by_kind"]("Component")[0]
    assert (comp["attributes"]["component_kind"], comp["attributes"]["kind_status"]) == ("unknown", "unknown")
    assert ("Component", "component_kind", "decision abstained: ambiguous_no_model") in plain["unknowns"]

    gw, model = gateway_with(lambda state, options: "service")
    decided = _compile(tmp_path, "F08-ai-agent", gateway=gw, name="modelled")
    comp = decided["by_kind"]("Component")[0]
    assert (comp["attributes"]["component_kind"], comp["attributes"]["kind_status"]) == ("service", "inferred"), "a model's answer is inferred, never supported"
    assert not any(u[1] == "component_kind" for u in decided["unknowns"])
    assert model.calls and model.calls[0][-1] == NONE_OF_THESE
    ledger = decided["store"].get_json(decided["result"].manifest.ledger_entries_ref)
    assert comp["attributes"]["decision_ref"] in {e["decision_id"] for e in ledger}
    assert decided["result"].manifest.toolchain_hash != plain["result"].manifest.toolchain_hash, "the model in use is part of the toolchain"

    calls = len(model.calls)
    replay = compile_commit(decided["store"], tmp_path / "repo-F08-ai-agent", REPO_ID, decided["result"].manifest.commit_sha, gateway=gw)
    assert len(model.calls) == calls and replay.manifest.rkp_hash == decided["result"].manifest.rkp_hash, "replay reuses the ledger: no model call, same hash"

    cold_gw, _ = gateway_with(lambda state, options: "service")
    cold = _compile(tmp_path, "F08-ai-agent", gateway=cold_gw, name="cold")
    assert cold["result"].manifest.rkp_hash == decided["result"].manifest.rkp_hash


def test_misleading_directory_name_is_not_evidence(tmp_path):
    g = _compile(tmp_path, "F10-ambiguous-incomplete", commit=1)
    helper = next(e for e in g["by_kind"]("Component") if e["natural_key"]["root"] == "service")
    assert helper["attributes"]["component_kind"] == "unknown"
    assert any(u[:2] == ("Repository", "component_boundary") for u in g["unknowns"]), "loose source files are reported, not silently skipped"


def test_ai_integration_and_deployment_links_are_graded(tmp_path):
    ai = _compile(tmp_path, "F08-ai-agent")
    link = ai["edges"][("AIComponent", "anthropic client", "CONSUMES", "claude-sonnet-5-5")]
    assert (link["resolution"], link["evidence_class"], link["epistemic_status"]) == ("candidate", "E4", "inferred")
    assert [e["natural_key"]["path"] for e in ai["by_kind"]("PromptArtifact")] == ["prompts/system.md"]
    assert ("Component", "support-agent", "CONTAINS", "anthropic client") in ai["edges"]

    infra = _compile(tmp_path, "F06-iac-heavy")
    deployed = infra["edges"][("Component", "status-api", "DEPLOYED_AS", "status-api")]
    assert (deployed["resolution"], deployed["epistemic_status"], deployed["qualifiers"]) == ("candidate", "inferred", {"basis": "name_match"})
    assert not any("production" in e["name"].lower() or "observed" in e["name"].lower() for e in infra["entities"].values()), "declared deployment is never recorded as observed"


def test_rate_limit_is_retried_within_a_bound():
    seen, waits = [], []
    def handler(request):
        seen.append(1)
        if len(seen) < 3:
            return httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"code": "RateLimitReached"}})
        return httpx.Response(200, json={"model": "m", "answers": {"decision": {"type": "choice", "choice": "a", "probabilities": {"a": 0.9, "b": 0.1}}}})
    client = jev("k", transport=httpx.MockTransport(handler), sleep=waits.append)
    assert client.choose("s", "Which?", {"a": "A", "b": "B"}).choice == "a" and waits == [7.0, 7.0]
    seen.clear()
    always = jev("k", transport=httpx.MockTransport(lambda r: httpx.Response(429, json={})), sleep=waits.append, max_retries=2)
    with pytest.raises(ModelUnavailable):
        always.choose("s", "Which?", {"a": "A", "b": "B"})


def test_rerank_adapter_maps_options_to_documents_and_normalises_scores():
    from repo_intel.decisions.gateway import cohere_rerank_azure, render_state
    sent = []
    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"id": "x", "results": [{"index": 1, "relevance_score": 0.06}, {"index": 0, "relevance_score": 0.02}, {"index": 2, "relevance_score": 0.02}], "meta": {"billed_units": {"input_tokens": 50}}})
    client = cohere_rerank_azure("https://res.example.azure.com/", "rerank-deploy", api_key="k-test", transport=httpx.MockTransport(handler))
    a = client.choose({"trust": "untrusted_data", "features": {"evidence": {"http_endpoints_declared_in_code": 0}, "names": ["x"]}}, "Which kind?", {"service": "Serves requests.", "library": "Imported.", "none_of_these": "Neither."})
    body = json.loads(sent[0].content)
    assert str(sent[0].url) == "https://res.example.azure.com/providers/cohere/v2/rerank" and sent[0].headers["api-key"] == "k-test"
    assert body["model"] == "rerank-deploy" and body["top_n"] == 3 and body["documents"] == ["service: Serves requests.", "library: Imported.", "none of these: Neither."]
    assert body["query"].startswith("Which kind?") and "- names: x" in body["query"] and "http_endpoints_declared_in_code" not in body["query"], "zero and false facts are left out"
    assert a.choice == "library" and a.probabilities == pytest.approx({"service": 0.2, "library": 0.6, "none_of_these": 0.2}) and a.input_tokens == 50
    assert render_state({"a": {"b": True}, "c": ["x"], "d": []}) == "a:\n  b: true\nc:\n  - \"x\"\nd: []"
    with pytest.raises(ModelUnavailable):
        cohere_rerank_azure("https://r", "d", api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 1}]}))).choose("s", "q", {"a": "A", "b": "B"})
    with pytest.raises(ValueError):
        cohere_rerank_azure("https://r", "d")
