"""Multi-repository golden fixtures (docs/solution-ir-contract.md 8.4).

F23 is a solution: its base holds `.rkp/solution.json`, two member repositories and the
orders database's catalogue. `golden_check` compiles each member on its own, loads the
solution and checks member-qualified entities and edges, identity links, unmatched
references and the solution's coverage, at the base and after each commit. These tests show the check
passes on F23 and fails when the golden is wrong in each of those ways.
"""

import copy
import json

import pytest

from repo_intel.compile import golden_check
from repo_intel.compile.fixtures import FIXTURES_DIR, list_fixtures, load_golden
from repo_intel.compile.ontology import EntityKind, check_relation
from repo_intel.solution import identity, parse_manifest

F23 = "F23-solution-multi-repo"
MILESTONE = golden_check.COMPILER_MILESTONE


@pytest.fixture(scope="module")
def commits(tmp_path_factory):
    return golden_check.compile_solution(F23, tmp_path_factory.mktemp("f23"))


@pytest.fixture
def golden():
    return copy.deepcopy(load_golden(F23))


def _problems(golden, commits):
    return golden_check.check_solution(golden, commits, MILESTONE)


def _end(member, kind, key):
    return {"member": member, "kind": kind, "natural_key": key}


def _link(rule, source, target):
    return {"link": {"rule": rule, "from": source, "to": target}, "since": "M2"}


ORDERS_TABLE = _end("orders", "Table", {"datastore": "default", "name": "orders"})
LOG_LEVEL = {"name": "LOG_LEVEL"}


def test_only_f23_is_a_multi_repository_fixture():
    assert [name for name in list_fixtures() if golden_check.is_solution(name)] == [F23]


def test_f23_meets_its_golden(golden, commits):
    assert _problems(golden, commits) == []
    assert [c["problems"] for c in commits] == [[], [], [], []]
    assert set(commits[0]["members"]) == {"web", "orders", "inputs"}


def test_each_member_is_compiled_in_its_own_repository(commits):
    base = commits[0]["members"]
    assert (base["web"]["repository_id"], base["orders"]["repository_id"]) == ("local/web", "local/service")
    kinds = {m: {e["kind"] for e in data["entities"].values()} for m, data in base.items()}
    assert "Endpoint" not in kinds["web"] and "Endpoint" in kinds["orders"], "the front end serves nothing"
    # A member that the commit leaves alone compiles to the same snapshot.
    assert commits[0]["members"]["web"]["rkp_hash"] == commits[2]["members"]["web"]["rkp_hash"] != commits[3]["members"]["web"]["rkp_hash"]
    assert len({c["members"]["orders"]["rkp_hash"] for c in commits}) == 3


def test_a_wrong_link_fails(golden, commits):
    customers = _end("inputs", "Table", {"datastore": "default", "name": "customers"})
    golden["links"].append(_link("same_table", ORDERS_TABLE, customers))
    assert _problems(golden, commits) == [
        'missing link same_table orders:Table {"datastore": "default", "name": "orders"} -> inputs:Table {"datastore": "default", "name": "customers"}',
    ]


def test_a_candidate_claimed_as_an_exact_link_fails(golden, commits):
    """LOG_LEVEL in both members is a lead (config_key_name_only), not the same key."""
    golden["links"].append(_link("same_config_key", _end("orders", "ConfigKey", LOG_LEVEL), _end("web", "ConfigKey", LOG_LEVEL)))
    [problem] = _problems(golden, commits)
    assert problem.startswith("missing link same_config_key orders:ConfigKey")


def test_a_link_to_the_wrong_member_fails(golden, commits):
    golden["links"].append(_link("same_table", ORDERS_TABLE, {**ORDERS_TABLE, "member": "web"}))
    assert len(_problems(golden, commits)) == 1


def test_an_absent_link_that_is_present_fails(golden, commits):
    golden["absent_links"].append({**_link("config_key_name_only", _end("orders", "ConfigKey", LOG_LEVEL), _end("web", "ConfigKey", LOG_LEVEL)), "why": "test"})
    [problem] = _problems(golden, commits)
    assert problem.startswith("link present but must be absent: config_key_name_only")


def test_an_entity_is_checked_in_the_member_it_names(golden, commits):
    endpoint = {"kind": "Endpoint", "natural_key": {"component_root": "", "method": "GET", "protocol": "http", "route": "/api/orders"}, "since": "M2"}
    golden["entities"].append({**endpoint, "member": "orders"})
    assert _problems(golden, commits) == []
    golden["entities"].append({**endpoint, "member": "web"})
    assert _problems(golden, commits) == ['missing entity web:Endpoint {"component_root": "", "method": "GET", "protocol": "http", "route": "/api/orders"}']
    golden["entities"][-1] = endpoint  # no member: any member will do
    assert _problems(golden, commits) == []


def test_a_wrong_attribute_or_edge_fails(golden, commits):
    golden["entities"].append({"member": "orders", "kind": "Table", "natural_key": {"datastore": "default", "name": "orders"}, "attributes": {"orm": "django"}, "since": "M2"})
    golden["edges"].append({"member": "web", "subject": {"kind": "Component", "natural_key": {"root": ""}}, "predicate": "READS_FROM", "object": {"kind": "Table", "natural_key": {"datastore": "default", "name": "orders"}}, "since": "M2"})
    assert _problems(golden, commits) == [
        "entity orders:Table {\"datastore\": \"default\", \"name\": \"orders\"}: attribute orm='sqlalchemy', want 'django'",
        'missing edge web:Component {"root": ""} -READS_FROM-> web:Table {"datastore": "default", "name": "orders"}',
    ]


def test_unmatched_references_and_coverage_are_checked(golden, commits):
    golden["unmatched"].append({"member": "web", "kind": "http_call", "reference": "/api/orders", "reason": "no_endpoint", "since": "M2"})
    [calls] = [c for c in golden["solution_coverage"] if c["domain"] == "solution_http_calls"]
    calls["numerator"] = 4
    golden["solution_coverage"].append({"domain": "solution_http_calls", "scope": "orders", "numerator": 0, "denominator": 0, "since": "M2"})
    assert _problems(golden, commits) == [
        "missing unmatched reference web: http_call /api/orders (no_endpoint)",
        "coverage solution_http_calls web: (2, 4), want (4, 4)",
        "coverage solution_http_calls orders: absent, want (0, 0)",
    ]


def test_a_call_that_states_its_method_links_exactly(golden, commits):
    """The front end's POST to /api/orders links to POST /api/orders by method and path; the
    listing call to the same path, method unstated, stays a candidate for both endpoints."""
    by_rule = {}
    for record in commits[0]["resolution"]["links"] + commits[0]["resolution"]["candidates"]:
        if record["qualifiers"].get("path") == "/api/orders":
            by_rule.setdefault(record["rule"], []).append((record["to_name"], record["resolution"], record["qualifiers"]["method"]))
    assert {rule: sorted(found) for rule, found in by_rule.items()} == {
        "endpoint_method_path": [("POST /api/orders", "exact", "POST")],
        "endpoint_method_unstated": [("GET /api/orders", "candidate", "unstated"), ("POST /api/orders", "candidate", "unstated")],
    }
    pinned = [x["link"] for x in golden["links"] if x["link"]["rule"] == "endpoint_method_path"]
    assert [link["to"]["natural_key"]["method"] for link in pinned] == ["POST"]


def test_commit_expectations_are_checked(golden, commits):
    first, _, third = golden["commits"]
    first["added_links"].append(_link("same_table", ORDERS_TABLE, _end("inputs", "Table", {"datastore": "default", "name": "orders"})))  # there from the base
    third["removed_links"] = []
    third["added_unmatched"] = [{"member": "web", "kind": "http_call", "reference": "/api/customers", "reason": "no_endpoint", "since": "M2"}]
    third["removed_entities"].append({"member": "orders", "kind": "ConfigKey", "natural_key": LOG_LEVEL, "since": "M2"})
    [calls] = [c for c in first["solution_coverage"] if c["domain"] == "solution_http_calls"]
    calls["numerator"] = 2  # the base's figure: the commit links /api/customers
    assert _problems(golden, commits) == [
        '01-service-serves-customers: should add link same_table orders:Table {"datastore": "default", "name": "orders"} -> inputs:Table {"datastore": "default", "name": "orders"}',
        "01-service-serves-customers: coverage solution_http_calls web: (3, 4), want (2, 4)",
        '03-web-drops-log-level: should remove orders:ConfigKey {"name": "LOG_LEVEL"}',
        "03-web-drops-log-level: should add unmatched reference web: http_call /api/customers (no_endpoint)",
    ]


def test_coverage_is_pinned_at_every_commit(golden, commits):
    """The golden gives the solution's coverage at the base and after each commit."""
    pinned = [golden["solution_coverage"]] + [spec["solution_coverage"] for spec in golden["commits"]]
    assert len(pinned) == len(commits)
    for want, commit in zip(pinned, commits, strict=True):
        got = {(c["domain"], c["scope"]): (c["numerator"], c["denominator"]) for c in commit["resolution"]["coverage"]}
        assert {(c["domain"], c["scope"]): (c["numerator"], c["denominator"]) for c in want} == got


def test_forbidden_kinds_and_load_problems_are_reported(golden, commits):
    golden["forbidden_kinds"] = ["Endpoint"]
    broken = copy.deepcopy(commits)
    broken[2]["problems"] = ["member orders: the bundle does not match its manifest"]
    assert _problems(golden, broken) == [
        "commit 2: solution: member orders: the bundle does not match its manifest",
        *[f"commit {i}: forbidden kinds present: ['Endpoint']" for i in range(4)],
    ]


def test_kinds_restrict_the_check(golden, commits):
    golden["links"].append(_link("same_table", ORDERS_TABLE, _end("inputs", "Table", {"datastore": "default", "name": "customers"})))
    golden["unmatched"].append({"member": "web", "kind": "http_call", "reference": "/nowhere", "reason": "no_endpoint", "since": "M2"})
    assert golden_check.check_solution(golden, commits, MILESTONE, {"Endpoint"}) == []
    assert len(golden_check.check_solution(golden, commits, MILESTONE, {"Table"})) == 1


def test_items_not_yet_due_are_skipped(golden, commits):
    golden["links"].append({**_link("same_table", ORDERS_TABLE, ORDERS_TABLE), "since": "M3"})
    assert _problems(golden, commits) == []


def test_the_compile_is_reproducible(commits, tmp_path):
    again = golden_check.compile_solution(F23, tmp_path)
    for before, after in zip(commits, again, strict=True):
        assert {m: d["rkp_hash"] for m, d in before["members"].items()} == {m: d["rkp_hash"] for m, d in after["members"].items()}
        assert json.dumps(before["resolution"], sort_keys=True) == json.dumps(after["resolution"], sort_keys=True)


def test_check_compiles_the_solution_when_given_a_single_repository_compile():
    """tests/test_fixtures.py compiles every fixture's tree as one repository and passes that
    to `check`; for a solution fixture `check` compiles the members itself."""
    assert golden_check.check(F23, [{"entities": {}, "assertions": []}], MILESTONE) == []


def test_the_multi_repository_golden_is_well_formed():
    g = load_golden(F23)
    manifest = parse_manifest((FIXTURES_DIR / F23 / "base" / ".rkp" / "solution.json").read_text())
    members = {m.id for m in manifest.members} | {"inputs"}
    commit_specs = g["commits"]
    entries = g["entities"] + g["absent_entities"] + [e for c in commit_specs for e in c["added_entities"] + c["removed_entities"]]
    links = g["links"] + g["absent_links"] + [x for c in commit_specs for x in c.get("added_links", []) + c.get("removed_links", [])]
    ends = entries + [x["link"][side] for x in links for side in ("from", "to")]
    for end in ends:
        EntityKind(end["kind"])
        assert end.get("member", "inputs") in members, end
    for x in links:
        assert f" {x['link']['rule']} " in f" {identity.__doc__.replace(chr(10), ' ')} ", f"unknown identity rule {x['link']['rule']}"
        assert ("since" in x) != ("why" in x)
    for edge in g["edges"]:
        assert edge["member"] in members
        assert check_relation(edge["predicate"], EntityKind(edge["subject"]["kind"]), EntityKind(edge["object"]["kind"])) is None
    for item in g["unmatched"] + [u for c in commit_specs for u in c.get("added_unmatched", []) + c.get("removed_unmatched", [])]:
        assert item["member"] in members and item["reason"]
    for item in g["solution_coverage"] + [x for c in commit_specs for x in c.get("solution_coverage", [])]:
        assert item["domain"] in {"solution_data_refs", "solution_http_calls"} and item["scope"] in members
        assert 0 <= item["numerator"] <= item["denominator"] and "since" in item


# ------------------------------------------------------------------ coverage per Explorer plane (8.4)


def test_the_explorer_lists_every_pack_with_experience_terms():
    """The experience plane shows `platform_constructs` for the packs listed in
    explorer/lib/plane-coverage.ts; a pack with a term for an experience kind must be there."""
    import re
    from pathlib import Path

    from repo_intel import platforms

    explorer = Path(__file__).resolve().parents[1] / "explorer" / "lib"
    source = (explorer / "plane-coverage.ts").read_text()
    listed = set(re.findall(r'"([a-z0-9_-]+)"', source[source.index("export const UI_PLATFORMS"):].split(";", 1)[0]))
    planes = (explorer / "planes.ts").read_text()
    experience = planes[planes.index('id: "experience"'):]
    kinds = set(re.findall(r'"([A-Za-z]+)"', experience[experience.index("kinds:"):experience.index("]")]))
    assert kinds == {"Screen", "Form", "UIControl", "StyleToken", "Report"}
    with_ui = {pack.id for pack in platforms.packs().values() if any(t.core.value in kinds for t in pack.terms)}
    assert with_ui and with_ui <= listed, f"add {sorted(with_ui - listed)} to UI_PLATFORMS"
    assert "catalog" not in listed
