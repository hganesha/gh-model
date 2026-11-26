"""Contract 8.2: business rules, workflows and captured screens, with a scripted model.

Candidates, workflow states and screen regions are deterministic; a model only names
rules, workflows and regions, behind the egress policy, and abstains on any failure.
"""

import json
import subprocess
from pathlib import Path

import pytest

from conftest import REPO_ID
from repo_intel.compile.contracts import Entity
from repo_intel.compile.ontology import EntityKind
from repo_intel.compile.pipeline import compile_commit, load_shards
from repo_intel.decisions import rules as dm_rules
from repo_intel.decisions import screens as dm_screens
from repo_intel.decisions import workflows as dm_workflows
from repo_intel.decisions.gateway import Gateway, ModelUnavailable, ScriptedLlm
from repo_intel.decisions.overrides import DecisionOverride, OverrideDocument, Overrides
from repo_intel.decisions.runner import Ledger
from repo_intel.knowledge.bundle import _architecture_shards
from repo_intel.storage.store import ObjectStore
from repo_intel.workspace.settings import CompileSettings

ON = CompileSettings(business_rules=True, captured_screens=True)

CFML = {
    "orders/approve.cfm": (
        '<cfquery name="current" datasource="shop">\n'
        '  SELECT status FROM orders WHERE order_id = <cfqueryparam value="#url.id#" cfsqltype="cf_sql_integer">\n'
        "</cfquery>\n"
        '<cfif form.qty LT 1>\n'
        '  <cfthrow type="Validation" message="Quantity must be at least one">\n'
        "</cfif>\n"
        '<cfif current.status EQ "submitted">\n'
        '  <cfquery datasource="shop">\n'
        "    UPDATE orders SET status = 'approved' WHERE order_id = <cfqueryparam value=\"#url.id#\" cfsqltype=\"cf_sql_integer\">\n"
        "  </cfquery>\n"
        "</cfif>\n"
        '<cflocation url="list.cfm" addtoken="no">\n'
    ),
    "orders/new.cfm": (
        '<cfquery datasource="shop">\n'
        "  INSERT INTO orders (order_id, status) VALUES (<cfqueryparam value=\"#form.id#\" cfsqltype=\"cf_sql_integer\">, 'draft')\n"
        "</cfquery>\n"
    ),
    "orders/submit.cfm": (
        '<cfquery datasource="shop">\n'
        '  UPDATE orders SET status = <cfqueryparam value="submitted" cfsqltype="cf_sql_varchar">\n'
        "  WHERE order_id = <cfqueryparam value=\"#form.id#\" cfsqltype=\"cf_sql_integer\"> AND status = 'draft'\n"
        "</cfquery>\n"
        '<cfquery datasource="shop">\n'
        '  UPDATE orders SET status = <cfqueryparam value="#form.next#" cfsqltype="cf_sql_varchar">\n'
        '  WHERE order_id = <cfqueryparam value="#form.id#" cfsqltype="cf_sql_integer">\n'
        "</cfquery>\n"
    ),
    "orders/edit.cfm": (
        '<cfform name="orderForm" action="approve.cfm" method="post">\n'
        '  <cfinput name="qty" type="text">\n'
        '  <cfinput type="submit" name="save" value="Save">\n'
        "</cfform>\n"
    ),
    "orders/list.cfm": (
        '<cfquery name="orders" datasource="shop">SELECT order_id, status FROM orders WHERE status <> \'archived\'</cfquery>\n'
        '<form name="filter" action="list.cfm"><input name="q"></form>\n'
        '<cfoutput query="orders"><a href="edit.cfm?id=#order_id#">#order_id#</a></cfoutput>\n'
    ),
}

SAS = {
    "sas/review.sas": (
        "data mart.orders;\n"
        "  set raw.orders;\n"
        "  where amount > 0;\n"
        "  if amount > 1000 then status = 'review';\n"
        "  else status = 'approved';\n"
        "  total = amount * 1.2;\n"
        "run;\n"
        "%macro mark(s);\n"
        "data mart.order_marks;\n"
        '  status = "&s";\n'
        "run;\n"
        "%mend mark;\n"
    ),
}

CAPTURE = {
    ".rkp/inputs/screens/capture.json": json.dumps({"captures": [
        {"template": "orders/list.cfm", "image": "list.png", "dom": "list.html"},
        {"route": "/nowhere", "dom": "list.html"},
    ]}, indent=1),
    ".rkp/inputs/screens/list.html": (
        "<!doctype html><html><body>\n"
        "<header><h1>Order desk</h1></header>\n"
        '<nav aria-label="Main"><a href="/orders/list.cfm?page=2">Orders</a> <a href="mailto:desk@example.com">Mail</a></nav>\n'
        "<main><h2>Open orders</h2>\n"
        '<form name="filter"><input name="q" value="typed-by-a-user"><select></select><input type="submit" value="Go"></form>\n'
        "<table><tr><td>1001</td></tr></table>\n"
        "</main>\n"
        "<footer>Shop</footer>\n"
        "</body></html>\n"
    ),
}


def _repo(path: Path, files: dict[str, str | bytes]) -> str:
    for name, content in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            (path / name).write_bytes(content)
        else:
            (path / name).write_text(content)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(path), *cmd], check=True, env=env)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


RULE_NAMES = {"Quantity": ("quantity-at-least-one", "validation", "An order needs a quantity of at least one."),
              "archived": ("hide-archived-orders", "decision", "Archived orders are not listed."),
              "1000": ("large-orders-need-review", "decision", "Orders over 1000 go to review; others are approved."),
              "amount > 0": ("positive-amounts-only", "validation", "Only orders with a positive amount are reviewed."),
              "total": ("total-with-tax", "calculation", "The total is the amount plus 20 percent.")}


class FakeModel:
    """Answers each kind of request the way a careful model would, from the data alone."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, data):
        self.calls.append(data)
        if data["scope"] == "owner":
            rules = []
            for c in data["features"]["candidates"]:
                text = (c.get("condition") or "") + " " + c["effect"]
                for marker, (name, kind, statement) in RULE_NAMES.items():
                    if marker in text:
                        rules.append({"name": name, "statement": statement, "rule_kind": kind, "candidate_ids": [c["id"]]})
            return {"rules": rules}
        if data["scope"] == "workflow":
            return {"name": f"{data['features']['table']} lifecycle", "states": [{"value": s, "label": s.capitalize()} for s in data["features"]["states"]], "evidence_ids": data["evidence_ids"][:1]}
        if data["scope"] == "screen_region":
            options = list(data["options"])
            return {"label": "results" if "results" in options else options[0], "evidence_ids": data["evidence_ids"]}
        return {"hypotheses": []}  # DM08, which the same setting turns on


def _gateway(model, allowed=("scripted",)):
    llm = ScriptedLlm(model)
    return Gateway({}, frozenset(allowed), {"llm-default": llm}), llm


def _compile(tmp_path, files, *, gateway=None, policy=ON, overrides=None, store=None, name="repo"):
    repo = tmp_path / name
    if not repo.exists():
        commit = _repo(repo, files)
    else:
        commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    store = store or ObjectStore(tmp_path / f"{name}-store", "t")
    result = compile_commit(store, repo, REPO_ID, commit, gateway=gateway, policy=policy, overrides=overrides)
    shards = load_shards(store, result.manifest)
    return store, result, shards


def _entities(shards, kind):
    return [e for s in shards.values() for e in s["entities"] if e["kind"] == kind]


def _assertions(shards, predicate):
    return [a for s in shards.values() for a in s["assertions"] if a["predicate"] == predicate]


def _coverage(store, result, domain, scope):
    return next(c for c in store.get_json(result.manifest.coverage_ref) if c["domain"] == domain and c["scope"] == scope)


# ---------------------------------------------------------------- candidates (no model)


def _lit(v, t="string"):
    return {"e": "lit", "value": v, "type": t}


def _var(name, scope=None):
    return {"e": "var", "name": name, **({"scope": scope} if scope else {})}


def test_candidates_come_from_guards_filters_and_computed_values():
    cfml = [
        {"op": "if", "at": [0, 50], "cond": {"e": "binop", "op": "<", "left": _var("qty", "form"), "right": _lit(1, "number")}, "then": [{"op": "throw", "at": [10, 40], "value": _lit("Quantity must be positive")}]},
        {"op": "if", "at": [60, 90], "cond": {"e": "binop", "op": "==", "left": _var("mode", "url"), "right": _lit("x")}, "then": [{"op": "output", "at": [70, 80], "channel": "response", "value": _lit("<p>hello</p>")}]},
        {"op": "query", "at": [100, 200], "sql": "SELECT * FROM orders o JOIN lines l ON o.id = l.order_id WHERE o.id = :id AND o.status <> 'archived'", "params": [{"name": "id", "value": _var("id", "url")}]},
        {"op": "query", "at": [210, 260], "sql": "SELECT id FROM orders WHERE id = :id", "params": []},
        {"op": "query", "at": [270, 330], "sql": "SELECT CASE WHEN amt > 100 THEN 'big' ELSE 'small' END AS band FROM t", "params": []},
        {"op": "query", "at": [340, 400], "sql": "SELECT * FROM remote_t WHERE x > 1", "params": [], "remote": "teradata"},
    ]
    found = dm_rules.unit_candidates(cfml)
    assert [(c["kind"], c["at"]) for c in found] == [("guard", [0, 50]), ("sql_where", [100, 200]), ("sql_case", [270, 330])], "plain output, key lookups and remote SQL are not candidates"
    assert found[0]["condition"] == "form.qty < 1" and found[0]["effect"] == 'throws "Quantity must be positive"'
    assert found[1]["condition"] == "o.status <> 'archived'" and found[1]["tables"] == ["lines", "orders"]
    assert found[2]["effect"] == "computes band"

    step = [{"op": "loop", "kind": "implicit", "at": [0, 300], "over": {"e": "array", "items": [{"e": "name", "kind": "dataset", "name": "RAW.Orders"}]}, "detail": {"data": ["mart.orders"]}, "body": [
        {"op": "if", "at": [10, 30], "where": True, "cond": {"e": "unop", "op": "not", "operand": {"e": "binop", "op": ">", "left": _var("amount", "pdv"), "right": _lit(0, "number")}}, "then": [{"op": "continue", "at": [10, 30]}]},
        {"op": "if", "at": [40, 60], "cond": {"e": "platform", "construct": "sas:first_last", "var": "id", "which": "first"}, "then": [{"op": "assign", "at": [50, 60], "target": _var("n", "pdv"), "value": _lit(0, "number")}]},
        {"op": "if", "at": [70, 120], "cond": {"e": "binop", "op": ">", "left": _var("amount", "pdv"), "right": _lit(1000, "number")}, "then": [{"op": "assign", "at": [80, 100], "target": _var("status", "pdv"), "value": _lit("review")}]},
        {"op": "assign", "at": [130, 160], "target": _var("total", "pdv"), "value": {"e": "binop", "op": "*", "left": _var("amount", "pdv"), "right": _lit(1.2, "number")}},
        {"op": "assign", "at": [170, 180], "target": _var("copy", "pdv"), "value": _var("amount", "pdv")},
    ]}]
    found = dm_rules.unit_candidates(step)
    assert [(c["kind"], c["condition"]) for c in found] == [("where", "amount > 0"), ("conditional_assignment", "amount > 1000"), ("computed", None)], "first./last. and plain copies are not rules"
    assert found[0]["datasets_read"] == ["raw.orders"] and found[2]["datasets_written"] == ["mart.orders"]
    assert found[2]["effect"] == "total = amount * 1.2"


def test_state_assignments_are_literal_or_dynamic():
    body = [
        {"op": "if", "at": [0, 100], "cond": {"e": "binop", "op": "==", "left": {"e": "member", "object": _var("q"), "name": "status"}, "right": _lit("submitted")},
         "then": [{"op": "query", "at": [10, 90], "sql": "UPDATE orders SET status = 'approved', qty = :q WHERE id = :id", "params": []}]},
        {"op": "query", "at": [110, 200], "sql": "UPDATE orders SET status = :s WHERE id = :id AND status IN ('approved', 'held')", "params": [{"name": "s", "value": _lit("shipped")}]},
        {"op": "query", "at": [210, 300], "sql": "UPDATE orders SET status = :s WHERE id = :id", "params": [{"name": "s", "value": _var("next", "form")}]},
        {"op": "query", "at": [310, 400], "sql": "INSERT INTO orders (id, status) VALUES (:id, 'draft')", "params": []},
    ]
    found = dm_workflows.unit_assignments(body)
    assert [(a["table"], a["value"], a["from"], a["kind"], a["dynamic"]) for a in found] == [
        ("orders", "approved", ["submitted"], "update", False),
        ("orders", "shipped", ["approved", "held"], "update", False),
        ("orders", None, [], "update", True),
        ("orders", "draft", [], "insert", False),
    ]
    assert found[0]["guard"] == 'q.status == "submitted"'


def test_sql_update_reads_from_states_in_where_in_and_bind_parameters():
    url_id = {"e": "var", "scope": "url", "name": "id"}
    body = [
        # A bind parameter with a literal value is a state; WHERE IN gives every from state.
        {"op": "query", "at": [0, 90], "sql": "UPDATE orders SET status = :next WHERE order_id = :id AND status IN ('submitted', 'held')",
         "params": [{"name": "next", "value": _lit("approved")}, {"name": "id", "value": url_id}]},
        # A bind parameter in the IN list counts when its value is a literal.
        {"op": "query", "at": [100, 190], "sql": "UPDATE orders SET status = 'closed' WHERE order_id = :id AND status IN ('approved', :was)",
         "params": [{"name": "was", "value": _lit("shipped")}, {"name": "id", "value": url_id}]},
        # `?` names no parameter: the new state is dynamic; the literal IN still gives the from state.
        {"op": "query", "at": [200, 290], "sql": "UPDATE orders SET status = ? WHERE order_id = ? AND status IN ('approved')", "params": []},
        # One value of the IN list is not literal (no such parameter): no from state is guessed;
        # the condition is kept as the transition's guard with the other non-key conditions.
        {"op": "query", "at": [300, 390], "sql": "UPDATE orders SET status = 'cancelled' WHERE order_id = :id AND status IN (:a, :b) AND amount > 100",
         "params": [{"name": "a", "value": _lit("draft")}]},
        # `status = :prior` with a literal value, a schema-qualified table and an upper-case column.
        {"op": "query", "at": [400, 490], "sql": "UPDATE sales.orders SET Status = :s, qty = 1 WHERE order_id = :id AND status = :prior",
         "params": [{"name": "s", "value": _lit("shipped")}, {"name": "prior", "value": _lit("approved")}]},
        # An inline parameter is text substituted into the SQL: never a state.
        {"op": "query", "at": [500, 590], "sql": "UPDATE orders SET status = :s WHERE order_id = 1", "params": [{"name": "s", "value": _var("s", "form"), "inline": True}]},
        {"op": "query", "at": [600, 690], "sql": "UPDATE orders SET status = 'x' WHERE status IN (:t)", "params": [{"name": "t", "value": _lit("y"), "inline": True}]},
    ]
    found = dm_workflows.unit_assignments(body)
    assert [(a["at"][0], a["table"], a["value"], a["dynamic"], a["from"], a["guard"]) for a in found] == [
        (0, "orders", "approved", False, ["held", "submitted"], None),
        (100, "orders", "closed", False, ["approved", "shipped"], None),
        (200, "orders", None, True, ["approved"], None),
        (300, "orders", "cancelled", False, [], "(status IN (:a, :b)) and (amount > 100)"),
        (400, "sales.orders", "shipped", False, ["approved"], None),
        (500, "orders", None, True, [], "order_id = 1"),
        (600, "orders", "x", True, [], "status IN (:t)"),
    ]


# ---------------------------------------------------------------- the model, held to its form


def _group():
    ev = {"id": "ev_a"}
    return {"owner": "ent_" + "a" * 26, "owner_kind": "Program", "owner_name": "approve.cfm", "unit": ".", "transforms": [], "omitted": 0, "evidence": [ev],
            "candidates": [{"id": "c1", "kind": "guard", "path": "a.cfm", "at": [0, 5], "unit_kind": "page", "condition": "form.qty < 1", "effect": "throws", "evidence_id": "ev_a", "applies_to": []}]}


def test_rules_are_kept_only_with_valid_names_kinds_and_citations(tmp_path, store):
    good = {"name": "Quantity At Least One", "statement": "Quantity must be at least one.", "rule_kind": "validation", "candidate_ids": ["c1"]}
    answers = {"rules": [good,
                         {**good, "name": "x" * 61},
                         {**good, "name": "other", "statement": "y" * 281},
                         {**good, "name": "made-up", "candidate_ids": ["c9"]},
                         {**good, "name": "wrong-kind", "rule_kind": "policy"},
                         {**good, "name": "no-citation", "candidate_ids": []}]}
    gw, llm = _gateway(lambda data: answers)
    d = dm_rules.lift(_group(), ledger=Ledger(store), gateway=gw, budget=None).row()
    assert [(r["name"], r["rule_kind"], r["candidate_ids"]) for r in dm_rules.rules_of(d)] == [("quantity-at-least-one", "validation", ["c1"])]
    assert d["hypotheses"][0]["evidence_ids"] == ["ev_a"] and d["model_ref"] == "scripted/scripted-llm-1"
    assert "trust" not in dm_rules.INSTRUCTIONS and llm.calls[0]["trust"] == "untrusted_data"

    bad, _ = _gateway(lambda data: {"rules": [{**good, "candidate_ids": ["c9"]}]})
    d = dm_rules.lift(_group(), ledger=Ledger(ObjectStore(tmp_path / "other", "t")), gateway=bad, budget=None)
    assert d.abstained and d.abstain_reason == "invalid_answer" and d.attempts == dm_rules.SPEC.max_attempts


def test_slugs_are_the_contract_form_or_nothing():
    assert dm_rules.slug("Quantity At Least One") == "quantity-at-least-one"
    assert dm_rules.slug("  over 1,000: review!  ") == "over-1-000-review"
    assert dm_rules.slug("a" * 60) == "a" * 60
    for bad in (None, 7, ["a"], "", "   ", "---", "!!!", "a" * 61, "ab-" * 20 + "c"):
        assert dm_rules.slug(bad) is None, bad


def test_each_failure_mode_of_a_rule_is_rejected_alone():
    by_id = {"c1": {}, "c2": {}, "c10": {}}
    good = {"name": "positive-quantity", "statement": "Quantity must be positive.", "rule_kind": "validation", "candidate_ids": ["c1"]}
    bad = {
        "name not a string": {"name": 12},
        "name empty once slugged": {"name": "***"},
        "name too long": {"name": "n" * 61},
        "kind unknown": {"rule_kind": "policy"},
        "kind missing": {"rule_kind": None},
        "statement too long": {"statement": "s" * 281},
        "statement blank": {"statement": "   "},
        "statement not a string": {"statement": ["Quantity must be positive."]},
        "citations not a list": {"candidate_ids": "c1"},
        "citations empty": {"candidate_ids": []},
        "citation unknown": {"candidate_ids": ["c9"]},
        "citation not a string": {"candidate_ids": [1]},
        "one citation unknown among good ones": {"candidate_ids": ["c1", "c9"]},
    }
    for why, change in bad.items():
        assert dm_rules._answered_rules({"rules": [{**good, **change}]}, by_id) == [], why
    assert dm_rules._answered_rules({"rules": "none"}, by_id) == [] and dm_rules._answered_rules(["x"], by_id) == []
    assert dm_rules._answered_rules({"rules": ["not an object", good]}, by_id) == [("validation", "positive-quantity", "Quantity must be positive.", ["c1"])]

    # The first rule of a name wins; citations are deduplicated and kept in candidate order;
    # the statement's whitespace is folded; rules come back sorted by name.
    answer = {"rules": [
        {**good, "name": "Zeta Rule", "statement": "Second\n  rule.", "candidate_ids": ["c10", "c2", "c10"]},
        good, {**good, "statement": "A later duplicate.", "candidate_ids": ["c2"]},
    ]}
    assert dm_rules._answered_rules(answer, by_id) == [
        ("validation", "positive-quantity", "Quantity must be positive.", ["c1"]),
        ("validation", "zeta-rule", "Second rule.", ["c2", "c10"]),
    ]


def test_an_owner_with_many_candidates_sends_the_first_and_counts_the_rest():
    from repo_intel.compile.buildgraph import StepInputs

    n = dm_rules.MAX_CANDIDATES + 7
    guards = [{"op": "if", "at": [i * 100, i * 100 + 50], "cond": {"e": "binop", "op": "<", "left": _var(f"v{i}", "form"), "right": _lit(1, "number")},
               "then": [{"op": "throw", "at": [i * 100 + 10, i * 100 + 40], "value": _lit(f"v{i} is required")}]} for i in range(n)]
    owner = {"id": "ent_owner", "kind": "Program", "name": "big.cfm", "natural_key": {"platform": "cfml", "name": "big.cfm"}, "attributes": {}, "evidence_ids": ["ev_1"]}
    artifact = {"id": "ent_file", "kind": "Artifact", "name": "big.cfm", "natural_key": {"path": "big.cfm"}, "attributes": {}, "evidence_ids": []}
    shard = {"unit": "@root", "entities": [owner, artifact], "assertions": [], "unknowns": [], "evidence": [{"id": "ev_1"}],
             "behaviors": [{"id": "b1", "owner": "ent_owner", "artifact": "ent_file", "kind": "page", "evidence_ids": ["ev_1"], "body": list(reversed(guards))}]}
    [group] = dm_rules._features(StepInputs(lambda ref: None, {"shards": {"@root": shard}}), {})["groups"]
    assert len(group["candidates"]) == dm_rules.MAX_CANDIDATES and group["omitted"] == 7
    assert [c["id"] for c in group["candidates"]] == [f"c{i}" for i in range(1, dm_rules.MAX_CANDIDATES + 1)]
    assert [c["at"][0] for c in group["candidates"]] == [i * 100 for i in range(dm_rules.MAX_CANDIDATES)], "the first in span order are kept"
    assert group["containers"] == [], "a Program owner has no container to add"
    sent = dm_rules.model_features(group)
    assert sent["omitted_candidates"] == 7 and len(sent["candidates"]) == dm_rules.MAX_CANDIDATES
    assert "evidence_id" not in sent["candidates"][0] and "applies_to" not in sent["candidates"][0], "internal references are not sent"


def test_lifting_abstains_on_denied_egress_and_on_model_failure(tmp_path, store):
    model = FakeModel()
    denied, llm = _gateway(model, allowed=())
    d = dm_rules.lift(_group(), ledger=Ledger(store), gateway=denied, budget=None)
    assert d.abstained and d.abstain_reason == "egress_denied" and llm.calls == [], "egress is denied by default"

    def down(data):
        raise ModelUnavailable("down")
    failing, _ = _gateway(down)
    d = dm_rules.lift(_group(), ledger=Ledger(store), gateway=failing, budget=None)
    assert d.abstained and d.abstain_reason == "model_unavailable"

    # In a compile: nothing model-written is published, while workflow states, which are facts, still are.
    store_, result, shards = _compile(tmp_path, CFML, gateway=denied)
    assert _entities(shards, "BusinessRule") == [] and model.calls == []
    [workflow] = _entities(shards, "Workflow")
    assert workflow["attributes"]["naming"] == "decision abstained: egress_denied" and "label" not in workflow["attributes"]
    reasons = _coverage(store_, result, "decisions", "@root")["detail"]["abstained_by_reason"]
    rows = [store_.get_json(e["digest"]) for e in store_.get_json(result.manifest.ledger_entries_ref)]
    lifts = [r for r in rows if r["spec"].split("@")[0] == dm_rules.SPEC.id]
    assert len(lifts) == 3 and all(r["abstain_reason"] == "egress_denied" for r in lifts)
    assert reasons["egress_denied"] == len(lifts), "each owner's lifting is a decision that abstained"


def test_off_unless_the_settings_turn_it_on(tmp_path):
    model = FakeModel()
    gw, llm = _gateway(model)
    store, result, shards = _compile(tmp_path, {**CFML, **CAPTURE}, gateway=gw, policy=None)
    assert llm.calls == []
    assert not _entities(shards, "BusinessRule") and not _entities(shards, "Workflow")
    assert not any("captured" in e["attributes"] for e in _entities(shards, "Screen"))
    assert not any(u["predicate"] in ("workflow_state", "captured_screen") for s in shards.values() for u in s["unknowns"])
    ledger = [store.get_json(e["digest"])["spec"] for e in store.get_json(result.manifest.ledger_entries_ref)]
    assert not any(s.split("@")[0] in (dm_rules.SPEC.id, dm_workflows.SPEC.id, dm_screens.SPEC.id) for s in ledger)


def test_each_feature_has_its_own_switch(tmp_path):
    files = {**CFML, **CAPTURE}
    assert not dm_rules.enabled(CompileSettings(behavior_candidates=True)), "DM08's switch is not the rules switch"
    assert not dm_rules.enabled(CompileSettings(behavior_candidates=True), "captured_screens")
    assert not dm_rules.enabled(None) and not dm_rules.enabled(None, "captured_screens")

    # DM08 alone: no rule, workflow or capture, and no 8.2 decision in the ledger.
    model = FakeModel()
    gw, _ = _gateway(model)
    store, result, shards = _compile(tmp_path, files, gateway=gw, policy=CompileSettings(behavior_candidates=True), name="dm08")
    assert not _entities(shards, "BusinessRule") and not _entities(shards, "Workflow")
    assert not any("captured" in e["attributes"] for e in _entities(shards, "Screen"))
    specs = {store.get_json(e["digest"])["spec"].split("@")[0] for e in store.get_json(result.manifest.ledger_entries_ref)}
    assert not specs & {dm_rules.SPEC.id, dm_workflows.SPEC.id, dm_screens.SPEC.id}

    # Rules alone lift rules and workflows, and capture nothing.
    _, _, shards = _compile(tmp_path, files, gateway=gw, policy=CompileSettings(business_rules=True), name="rules")
    assert _entities(shards, "BusinessRule") and _entities(shards, "Workflow")
    assert not any("captured" in e["attributes"] for e in _entities(shards, "Screen"))

    # Captures alone attach regions and lift nothing.
    _, _, shards = _compile(tmp_path, files, gateway=gw, policy=CompileSettings(captured_screens=True), name="screens")
    assert not _entities(shards, "BusinessRule") and not _entities(shards, "Workflow")
    assert any("captured" in e["attributes"] for e in _entities(shards, "Screen"))


# ---------------------------------------------------------------- compiled rules and workflows


def test_rules_and_workflows_from_cfml(tmp_path):
    model = FakeModel()
    gw, llm = _gateway(model)
    store, result, shards = _compile(tmp_path, CFML, gateway=gw)
    ents = {e["id"]: e for s in shards.values() for e in s["entities"]}
    rules = {e["natural_key"]["name"]: e for e in _entities(shards, "BusinessRule")}
    assert set(rules) == {"quantity-at-least-one", "hide-archived-orders"}
    qty = rules["quantity-at-least-one"]
    assert qty["natural_key"] == {"scope": "approve.cfm", "name": "quantity-at-least-one"}
    assert qty["id"] == Entity.make(REPO_ID, EntityKind.BUSINESS_RULE, qty["natural_key"], "quantity-at-least-one").id
    attrs = qty["attributes"]
    assert (attrs["statement"], attrs["rule_kind"], attrs["status"], attrs["trust_label"]) == ("An order needs a quantity of at least one.", "validation", "hypothesis", "untrusted_generated")
    assert attrs["candidates"][0]["kind"] == "guard" and attrs["candidates"][0]["path"] == "orders/approve.cfm"

    enforces = [a for a in _assertions(shards, "ENFORCES") if a["object"] == qty["id"]]
    assert [ents[a["subject"]]["name"] for a in enforces] == ["approve.cfm"]
    assert {(a["assertion_kind"], a["epistemic_status"], a["evidence_class"]) for a in enforces} == {("hypothesis", "inferred", "E5")}
    targets = {(ents[a["object"]]["kind"], ents[a["object"]]["name"]) for a in _assertions(shards, "APPLIES_TO") if a["subject"] == qty["id"]}
    assert ("Form", "orderForm") in targets, "form.qty is read from the form that submits to the page"
    archived = {(ents[a["object"]]["kind"], ents[a["object"]]["name"]) for a in _assertions(shards, "APPLIES_TO") if a["subject"] == rules["hide-archived-orders"]["id"]}
    assert ("Table", "orders") in archived

    [wf] = _entities(shards, "Workflow")
    assert wf["natural_key"] == {"scope": "orders", "name": "status"}
    w = wf["attributes"]
    assert w["states"] == ["approved", "draft", "submitted"] and w["status"] == "hypothesis"
    assert {(t["from"], t["to"], t["kind"]) for t in w["transitions"]} == {(None, "draft", "insert"), ("draft", "submitted", "update"), ("submitted", "approved", "update")}
    assert all(t["evidence_id"] in wf["evidence_ids"] for t in w["transitions"])
    assert w["label"] == "orders lifecycle" and w["state_labels"]["approved"] == "Approved"
    [applies] = [a for a in _assertions(shards, "APPLIES_TO") if a["subject"] == wf["id"]]
    assert ents[applies["object"]]["kind"] == "Table" and (applies["evidence_class"], applies["epistemic_status"]) == ("E3", "inferred")

    dynamic = [u for s in shards.values() for u in s["unknowns"] if u["predicate"] == "workflow_state"]
    assert len(dynamic) == 1 and "orders.status is assigned a value that is not a literal" in dynamic[0]["reason"], "form.next stays dynamic"
    assert dynamic[0]["coverage_ref"] == "behavior:@root" and ents[dynamic[0]["subject"]]["natural_key"] == {"path": "orders/submit.cfm"}

    # Rules and workflows are architecture-tier knowledge.
    conflicts = store.get_json(result.manifest.conflicts_ref)
    kept = {e["kind"] for s in _architecture_shards(shards, conflicts).values() for e in s["entities"]}
    assert {"BusinessRule", "Workflow"} <= kept

    calls = len(llm.calls)
    again = compile_commit(store, tmp_path / "repo", REPO_ID, result.manifest.commit_sha, gateway=gw, policy=ON)
    assert len(llm.calls) == calls and again.manifest.rkp_hash == result.manifest.rkp_hash, "replay is a ledger hit"


def test_workflows_and_rules_from_a_sas_data_step(tmp_path):
    gw, _ = _gateway(FakeModel())
    store, result, shards = _compile(tmp_path, SAS, gateway=gw)
    ents = {e["id"]: e for s in shards.values() for e in s["entities"]}
    [wf] = _entities(shards, "Workflow")
    assert wf["natural_key"] == {"scope": "mart.orders", "name": "status"} and wf["attributes"]["states"] == ["approved", "review"]
    guards = sorted(t["guard"] for t in wf["attributes"]["transitions"])
    assert guards == ["amount > 1000", "not (amount > 1000)"]
    [applies] = [a for a in _assertions(shards, "APPLIES_TO") if a["subject"] == wf["id"]]
    assert ents[applies["object"]]["kind"] == "Dataset" and ents[applies["object"]]["name"] == "mart.orders"
    assert any(u["predicate"] == "workflow_state" and "mart.order_marks.status" in u["reason"] for s in shards.values() for u in s["unknowns"]), "a macro variable is dynamic"

    rules = {e["natural_key"]["name"]: e for e in _entities(shards, "BusinessRule")}
    assert {"large-orders-need-review", "positive-amounts-only", "total-with-tax"} <= set(rules)
    assert rules["total-with-tax"]["attributes"]["rule_kind"] == "calculation"

    def targets(name):
        return {ents[a["object"]]["name"] for a in _assertions(shards, "APPLIES_TO") if a["subject"] == rules[name]["id"]}
    assert targets("positive-amounts-only") == {"raw.orders"}, "a WHERE reads the rows of the dataset the step reads"
    assert targets("total-with-tax") == {"mart.orders"}, "a computed column belongs to the dataset written"


# ---------------------------------------------------------------- owner confirmation


def test_owner_confirms_and_rejects_through_decision_overrides(tmp_path):
    with pytest.raises(ValueError):
        DecisionOverride(spec="dm.business-rules", scope_entity="ent_" + "a" * 26, label="maybe")

    model = FakeModel()
    gw, llm = _gateway(model)
    store, first, shards = _compile(tmp_path, CFML, gateway=gw)
    rules = {e["natural_key"]["name"]: e["id"] for e in _entities(shards, "BusinessRule")}
    [wf] = _entities(shards, "Workflow")
    calls = len(llm.calls)

    overrides = Overrides.of(OverrideDocument(decisions=[
        DecisionOverride(spec="dm.business-rules", scope_entity=rules["quantity-at-least-one"], label="confirmed"),
        DecisionOverride(spec="dm.business-rules", scope_entity=rules["hide-archived-orders"], label="rejected", reason="Archived orders are listed on another screen."),
        DecisionOverride(spec="dm.workflows", scope_entity=wf["id"], label="confirmed"),
    ]))
    _, second, shards = _compile(tmp_path, CFML, gateway=gw, overrides=overrides, store=store)
    assert len(llm.calls) == calls, "confirming does not ask the model again"
    assert second.manifest.override_set_hash == overrides.set_hash

    by_id = {e["id"]: e for e in _entities(shards, "BusinessRule")}
    assert rules["hide-archived-orders"] not in by_id, "a rejected rule leaves the snapshot"
    confirmed = by_id[rules["quantity-at-least-one"]]
    assert confirmed["attributes"]["status"] == "confirmed" and confirmed["attributes"]["confirmation_ref"].startswith("dec_")
    edges = [a for a in _assertions(shards, "ENFORCES") + _assertions(shards, "APPLIES_TO") if rules["quantity-at-least-one"] in (a["subject"], a["object"])]
    assert edges and {(a["epistemic_status"], a["verification_ref"], a["decision_ref"]) for a in edges} == {("supported", "owner_override", confirmed["attributes"]["confirmation_ref"])}

    conflicts = store.get_json(second.manifest.conflicts_ref)
    rejected = [c for c in conflicts if c["subject_ids"] == [rules["hide-archived-orders"]]]
    assert len(rejected) == 1 and rejected[0]["conflict_type"] == "label_disagreement" and rejected[0]["preferred"] == "owner_override:rejected"

    [wf2] = _entities(shards, "Workflow")
    assert wf2["attributes"]["status"] == "confirmed"
    assert {a["epistemic_status"] for a in _assertions(shards, "APPLIES_TO") if a["subject"] == wf2["id"]} == {"supported"}
    ledger = {e["decision_id"]: store.get_json(e["digest"]) for e in store.get_json(second.manifest.ledger_entries_ref)}
    owner_words = sorted(d["selected_label"] for d in ledger.values() if d["decision_method"] == "owner_override")
    assert owner_words == ["confirmed", "confirmed", "rejected"], "each confirmation is its own ledger decision"


def test_a_rejected_workflow_leaves_a_conflict_record(tmp_path):
    gw, _ = _gateway(FakeModel())
    store, _, shards = _compile(tmp_path, CFML, gateway=gw)
    [wf] = _entities(shards, "Workflow")
    overrides = Overrides.of(OverrideDocument(decisions=[DecisionOverride(spec="dm.workflows", scope_entity=wf["id"], label="rejected", reason="Status is a display field here.")]))
    _, second, shards = _compile(tmp_path, CFML, gateway=gw, overrides=overrides, store=store)
    assert _entities(shards, "Workflow") == [], "a rejected workflow leaves the snapshot"
    assert not [a for s in shards.values() for a in s["assertions"] if wf["id"] in (a["subject"], a["object"])], "and so do its edges"
    [conflict] = [c for c in store.get_json(second.manifest.conflicts_ref) if c["subject_ids"] == [wf["id"]]]
    assert conflict["conflict_type"] == "label_disagreement" and conflict["preferred"] == "owner_override:rejected"
    ledger = [store.get_json(e["digest"]) for e in store.get_json(second.manifest.ledger_entries_ref)]
    [word] = [d for d in ledger if d["decision_method"] == "owner_override"]
    assert (word["spec"].split("@")[0], word["scope_entity"], word["selected_label"]) == (dm_workflows.SPEC.id, wf["id"], "rejected")
    assert [(d["method"], d["label"]) for d in word["disagreements"]] == [("llm", "hypothesis")], "the compiler's word, overruled"
    assert set(conflict["evidence_ids"]) == set(word["evidence_ids"]) == set(wf["evidence_ids"])


def test_a_cold_compile_with_rules_on_is_deterministic(tmp_path):
    """Two compiles of one commit into two empty stores, each with its own model: the same
    snapshot, byte for byte, down to the hash that covers the decision ledger."""
    first_model, second_model = FakeModel(), FakeModel()
    files = {**CFML, **SAS, **CAPTURE}
    store, first, shards = _compile(tmp_path, files, gateway=_gateway(first_model)[0])
    assert _entities(shards, "BusinessRule") and _entities(shards, "Workflow"), "the comparison needs lifted knowledge"
    cold = ObjectStore(tmp_path / "cold-store", "t")
    second = compile_commit(cold, tmp_path / "repo", REPO_ID, first.manifest.commit_sha, gateway=_gateway(second_model)[0], policy=ON, use_cache=False)
    assert second_model.calls and len(second_model.calls) == len(first_model.calls), "the cold store asked its model the same questions"
    assert second.manifest.rkp_hash == first.manifest.rkp_hash
    assert second.manifest.content_hash == first.manifest.content_hash and second.manifest.provenance_hash == first.manifest.provenance_hash
    assert load_shards(cold, second.manifest) == shards


def test_a_rule_a_function_enforces_stays_attached_in_the_architecture_tier(tmp_path):
    files = {
        "services/orderservice.cfc": (
            '<cfcomponent output="false">\n'
            '  <cffunction name="approve" access="public" returntype="void">\n'
            '    <cfargument name="qty" type="numeric" required="true">\n'
            '    <cfif arguments.qty LT 1>\n'
            '      <cfthrow type="Validation" message="Quantity must be at least one">\n'
            "    </cfif>\n"
            "  </cffunction>\n"
            "</cfcomponent>\n"
        ),
    }
    gw, _ = _gateway(FakeModel())
    store, result, shards = _compile(tmp_path, files, gateway=gw)
    ents = {e["id"]: e for s in shards.values() for e in s["entities"]}
    [rule] = _entities(shards, "BusinessRule")
    enforces = sorted((ents[a["subject"]]["kind"], ents[a["subject"]]["name"], a["qualifiers"].get("via")) for a in _assertions(shards, "ENFORCES") if a["object"] == rule["id"])
    symbol, program = enforces[1], enforces[0]
    assert symbol[0] == "Symbol" and symbol[2] is None and rule["natural_key"]["scope"] == symbol[1]
    assert program[0] == "Program" and program[2] == "symbol", "the component enforces the rule through its function"
    contains = {(a["subject"], a["object"]) for a in _assertions(shards, "CONTAINS")}
    sym_id = next(i for i, e in ents.items() if e["kind"] == "Symbol" and e["name"] == symbol[1])
    prog_id = next(a["subject"] for a in _assertions(shards, "ENFORCES") if a["object"] == rule["id"] and a["qualifiers"].get("via") == "symbol")
    assert (prog_id, sym_id) in contains
    both = [a for a in _assertions(shards, "ENFORCES") if a["object"] == rule["id"]]
    assert len({(a["assertion_kind"], a["epistemic_status"], a["evidence_class"], a["decision_ref"]) for a in both}) == 1, "the container's edge is as strong as the Symbol's"

    conflicts = store.get_json(result.manifest.conflicts_ref)
    tier = _architecture_shards(shards, conflicts)
    kept = [a for s in tier.values() for a in s["assertions"] if a["predicate"] == "ENFORCES" and a["object"] == rule["id"]]
    assert [(a["subject"], a["qualifiers"]) for a in kept] == [(prog_id, {"basis": "rule_lifting", "via": "symbol"})]
    assert rule["id"] in {e["id"] for s in tier.values() for e in s["entities"]}


def test_a_symbol_without_a_container_falls_back_to_the_package_its_key_names():
    from repo_intel.decisions.rules import _Index

    symbol = {"id": "ent_sym", "kind": "Symbol", "name": "save", "natural_key": {"package": "shop", "qualified_name": "app.save"}}
    package = {"id": "ent_pkg", "kind": "Package", "name": "shop", "natural_key": {"ecosystem": "pypi", "name": "shop", "manifest_dir": ""}}
    other = {"id": "ent_pkg2", "kind": "Package", "name": "shop", "natural_key": {"ecosystem": "npm", "name": "shop", "manifest_dir": "web"}}
    empty = {"assertions": [], "evidence": [], "unknowns": []}
    index = _Index({"@root": {**empty, "unit": "@root", "entities": [symbol, package]}, "web": {**empty, "unit": "web", "entities": [other]}})
    assert index.symbol_containers("ent_sym") == ["ent_pkg"], "only the Package of the Symbol's own unit"
    index = _Index({"@root": {**empty, "unit": "@root", "entities": [symbol, package, {**other, "id": "ent_pkg3"}]}})
    assert index.symbol_containers("ent_sym") == [], "two Packages of that name: none is chosen"
    index = _Index({"@root": {**empty, "unit": "@root", "entities": [symbol]}})
    assert index.symbol_containers("ent_sym") == [], "no container and no Package: the Symbol keeps only its own edge"


# ---------------------------------------------------------------- captured screens


def test_dom_regions_and_control_names():
    data = CAPTURE[".rkp/inputs/screens/list.html"].encode()
    regions = dm_screens.dom_regions(data, "orders/list.cfm")
    assert [(r["id"], r["landmark"], r["heading"]) for r in regions] == [
        ("r1", "banner", "Order desk"), ("r2", "navigation", None), ("r3", "main", "Open orders"), ("r4", "form", None), ("r5", "contentinfo", None)]
    assert regions[1]["aria_label"] == "Main"
    assert [c["name"] for c in regions[1]["controls"]] == ["link:/orders/list.cfm", "link1"], "a mailto link carries no target name"
    assert [(c["container"], c["name"]) for c in regions[3]["controls"]] == [("orders/list.cfm#filter", "q"), ("orders/list.cfm#filter", "select1"), ("orders/list.cfm#filter", "submit")]
    assert data[regions[0]["at"][0]:regions[0]["at"][1]].startswith(b"<header>") and data[regions[0]["at"][0]:regions[0]["at"][1]].endswith(b"</header>")
    assert "typed-by-a-user" not in json.dumps(regions), "input values are never read"


def test_captured_screens_attach_to_their_screen(tmp_path):
    files = {**CFML, **CAPTURE, ".rkp/inputs/screens/list.png": b"\x89PNG\r\n\x1a\n" + bytes(16)}
    model = FakeModel()
    gw, llm = _gateway(model)
    store, result, shards = _compile(tmp_path, files, gateway=gw)
    screens = {e["natural_key"]["name"]: e for e in _entities(shards, "Screen")}
    captured = screens["orders/list.cfm"]["attributes"]["captured"]
    assert len(captured) == 1
    c = captured[0]
    assert (c["capture"], c["entry"], c["dom"], c["image"], c["evidence_class"]) == (".rkp/inputs/screens/capture.json", 0, ".rkp/inputs/screens/list.html", ".rkp/inputs/screens/list.png", "E5")
    labels = [(r["landmark"], r["label"], r["label_status"]) for r in c["regions"]]
    assert labels == [("banner", "header", "supported"), ("navigation", "navigation", "supported"), ("main", "results", "inferred"), ("form", "filters", "inferred"), ("contentinfo", "footer", "supported")]
    assert set(c["evidence_ids"]) <= set(screens["orders/list.cfm"]["evidence_ids"])
    evidence = {ev["id"]: ev for s in shards.values() for ev in s["evidence"]}
    assert {evidence[i]["observation_kind"] for i in c["evidence_ids"]} == {"screen_capture", "screen_region"}
    region_calls = [d for d in llm.calls if d["scope"] == "screen_region"]
    assert len(region_calls) == 2, "landmarks settle header, navigation and footer without a model"

    [unmatched] = [u for s in shards.values() for u in s["unknowns"] if u["predicate"] == "captured_screen"]
    assert "route /nowhere" in unmatched["reason"] and "matches no Screen" in unmatched["reason"] and unmatched["coverage_ref"] == "decisions:@repo"
    assert _coverage(store, result, "decisions", "@repo")["detail"]["abstained_by_reason"]["capture_matches_no_screen"] == 1
    assert ".rkp/inputs/screens/list.html" in screens, "the web pack reads the saved DOM as a page (a foundation request)"
    assert "captured" not in screens[".rkp/inputs/screens/list.html"]["attributes"], "and a capture is never matched to it"
    assert "typed-by-a-user" not in json.dumps(shards)
