"""The ColdFusion (CFML) platform pack: parsing of tag and script syntax, behaviour, entities and
their natural keys, Administrator exports, and that it produces everything `legacy.cfml` did."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from repo_intel import extractors, platforms
from repo_intel.compile.behavior import validate_unit
from repo_intel.compile.fixtures import FIXTURES_DIR
from repo_intel.extractors.legacy import ColdFusion
from repo_intel.platforms.cfml import PACK
from repo_intel.platforms.cfml.admin import CfmlAdmin
from repo_intel.platforms.cfml.markup import parse as parse_markup
from repo_intel.platforms.cfml.source import CfmlSource, resolve_include, resolve_url

SOURCE, ADMIN = CfmlSource(), CfmlAdmin()
BLOB = "0" * 64
F15, F20 = FIXTURES_DIR / "F15-legacy-web-warehouse", FIXTURES_DIR / "F20-coldfusion-app"


def run(path: str, text: str | bytes, extractor=SOURCE):
    data = text.encode() if isinstance(text, str) else text
    return extractor.extract(path, data, BLOB)


def entities(out, kind=None):
    return [o.payload for o in out.observations if o.kind == "entity" and (kind is None or o.payload["kind"] == kind)]


def relations(out, predicate=None):
    return [o.payload for o in out.observations if o.kind == "relation" and (predicate is None or o.payload["predicate"] == predicate)]


def behaviours(out):
    return {o.payload["owner"]["name"]: o.payload for o in out.observations if o.kind == "behavior"}


def constructs(out):
    found: dict[str, list[bool]] = {}
    for o in out.observations:
        if o.kind == "construct":
            found.setdefault(o.payload["construct"], []).append(o.payload["modelled"])
    return found


def messages(out):
    return [d.message for d in out.diagnostics]


def keys(items):
    """Entities and relations as comparable (kind, key) tuples."""
    out = set()
    for o in items:
        p = o.payload
        if o.kind == "entity":
            out.add(("entity", p["kind"], json.dumps(p["natural_key"], sort_keys=True)))
        elif o.kind == "relation":
            s, ob = p["subject"], p["object"]
            out.add(("relation", p["predicate"], s["kind"], json.dumps(s["natural_key"], sort_keys=True), ob["kind"], json.dumps(ob["natural_key"], sort_keys=True)))
    return out


def cf_files(*roots: Path):
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in (".cfm", ".cfc"):
                yield root, path


def ops(body):
    """Every statement op in a behaviour body, depth first."""
    for st in body:
        yield st
        for key in ("body", "then", "else", "finally"):
            if isinstance(st.get(key), list):
                yield from ops(st[key])
        for case in st.get("cases", []):
            yield from ops(case.get("body", []))
        for catch in st.get("catches", []):
            yield from ops(catch.get("body", []))
        for branch in st.get("elseif", []):
            yield from ops(branch.get("body", branch.get("then", [])))


def walk(node):
    """Every dict nested in a behaviour body."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


# ------------------------------------------------------------------ the pack


def test_pack_is_registered_and_replaces_legacy():
    assert platforms.packs()["cfml"] is PACK
    assert platforms.validate_pack(PACK) == []
    assert "legacy.cfml" in platforms.superseded()
    ids = {e.id for e in extractors.EXTRACTORS}
    assert {"cfml.source", "cfml.admin"} <= ids and "legacy.cfml" not in ids
    assert PACK.inputs == (".rkp/inputs/cfml/**",)
    assert len(PACK.constructs) >= 60
    assert sum(1 for c in PACK.constructs if c.modelled_as == "unmapped") >= 10, "constructs not modelled are listed too"


def test_importing_the_pack_first_still_registers_it():
    """Pack modules must not import the extractors package at the top (discovery runs while it loads)."""
    root = Path(__file__).resolve().parents[1]
    code = "import repo_intel.platforms.cfml; from repo_intel import platforms, extractors; assert 'cfml' in platforms.packs(); assert 'cfml.source' in {e.id for e in extractors.EXTRACTORS}"
    env = {**os.environ, "PYTHONPATH": str(root)}
    subprocess.run([sys.executable, "-c", code], check=True, cwd=root, env=env)


def test_superset_of_legacy_on_f15():
    """Every entity and relation legacy.cfml produced on F15, with the same kinds and natural keys."""
    seen = 0
    for root, path in cf_files(F15 / "base", *(p for p in sorted((F15 / "commits").iterdir()) if p.is_dir())):
        rel, data = str(path.relative_to(root)), path.read_bytes()
        old, new = ColdFusion().extract(rel, data, BLOB), SOURCE.extract(rel, data, BLOB)
        missing = keys(old.observations) - keys(new.observations)
        assert not missing, f"{rel}: legacy produced {sorted(missing)}"
        seen += 1
    assert seen >= 5


@pytest.mark.parametrize("root", [F15, F20], ids=["F15", "F20"])
def test_every_unit_is_valid_and_every_construct_registered(root):
    registered = {c.id for c in PACK.constructs}
    layers = [root / "base", *(p for p in sorted((root / "commits").iterdir()) if p.is_dir())]
    for layer, path in cf_files(*layers):
        out = SOURCE.extract(str(path.relative_to(layer)), path.read_bytes(), BLOB)
        units = [o.payload for o in out.observations if o.kind == "behavior"]
        assert units, path
        for unit in units:
            assert validate_unit(unit) == [], (path, unit["owner"]["name"])
        assert set(constructs(out)) <= registered
        assert not [m for m in messages(out) if "not emitted" in m]


def test_a_behaviour_unit_for_every_function_and_page():
    out = run("orders/OrderService.cfc", (F20 / "base/services/OrderService.cfc").read_text())
    assert set(behaviours(out)) == {"getOrder", "saveOrder", "notify"}
    page = run("orders/list.cfm", (F20 / "base/orders/list.cfm").read_text())
    assert set(behaviours(page)) == {"list.cfm"}


# ------------------------------------------------------------------ entities and natural keys


def test_component_functions_endpoints_and_roles():
    out = run("services/OrderService.cfc", (F20 / "base/services/OrderService.cfc").read_text())
    program = entities(out, "Program")[0]
    assert program["natural_key"] == {"platform": "cfml", "name": "services.orderservice"}
    assert program["attributes"]["kind"] == "component" and program["attributes"]["platform_kind"] == "cf:Component"
    symbols = {e["natural_key"]["name"]: e for e in entities(out, "Symbol")}
    assert set(symbols) == {"getorder", "saveorder", "notify"}
    assert symbols["getorder"]["natural_key"] == {"platform": "cfml", "program": "services.orderservice", "name": "getorder"}
    assert symbols["getorder"]["attributes"]["roles"] == ["clerk", "manager"]
    endpoints = [e["natural_key"]["route"] for e in entities(out, "Endpoint")]
    assert endpoints == ["/services/OrderService.cfc?method=getOrder"]  # public saveOrder is not an endpoint
    assert {e["natural_key"]["name"] for e in entities(out, "Role")} == {"clerk", "manager"}
    assert all(e["natural_key"]["realm"] == "cfml" for e in entities(out, "Role"))
    needs = {(r["subject"]["kind"], r["object"]["natural_key"]["name"]) for r in relations(out, "REQUIRES_ROLE")}
    assert needs == {("Symbol", "clerk"), ("Symbol", "manager"), ("Endpoint", "clerk"), ("Endpoint", "manager")}
    calls = {(r["subject"]["name"], r["object"]["name"]) for r in relations(out, "CALLS")}
    assert ("saveOrder", "notify") in calls
    writes = {(r["subject"]["kind"], r["object"]["name"]) for r in relations(out, "WRITES_TO")}
    assert {("Program", "orders"), ("Symbol", "orders"), ("Symbol", "order_history")} <= writes


def test_screen_forms_controls_and_navigation():
    out = run("orders/edit.cfm", (F20 / "base/orders/edit.cfm").read_text())
    (screen,) = entities(out, "Screen")
    assert screen["natural_key"] == {"platform": "cfml", "name": "orders/edit.cfm"} and screen["attributes"]["fidelity"] == "lossy"
    (form,) = entities(out, "Form")
    assert form["natural_key"] == {"screen": "orders/edit.cfm", "name": "orderForm"} and form["attributes"]["kind"] == "cfform"
    controls = {e["natural_key"]["name"]: e for e in entities(out, "UIControl")}
    assert controls["qty"]["natural_key"] == {"container": "orders/edit.cfm#orderForm", "name": "qty"}
    assert controls["qty"]["attributes"]["validate"] == "integer" and controls["qty"]["attributes"]["required"] is True
    assert controls["status"]["attributes"]["control"] == "select" and controls["notes"]["attributes"]["control"] == "textarea"
    assert controls["link:list.cfm"]["natural_key"]["container"] == "orders/edit.cfm"  # outside the form
    (submit,) = relations(out, "SUBMITS_TO")
    assert submit["object"]["natural_key"] == {"platform": "cfml", "name": "orders/save.cfm"} and submit["object"]["declare"] is False
    (nav,) = relations(out, "NAVIGATES_TO")
    assert nav["object"]["natural_key"]["name"] == "orders/list.cfm"
    contains = {(r["subject"]["kind"], r["object"]["kind"]) for r in relations(out, "CONTAINS")}
    assert contains == {("Screen", "Form"), ("Form", "UIControl"), ("Screen", "UIControl")}
    assert ("Program", "Screen") in {(r["subject"]["kind"], r["object"]["kind"]) for r in relations(out, "RENDERS")}


def test_a_page_that_renders_nothing_is_not_a_screen():
    out = run("logout.cfm", (F20 / "base/logout.cfm").read_text())
    assert not entities(out, "Screen")
    assert entities(out, "Endpoint")


def test_form_targets_literal_remote_method_and_implicit_self():
    text = """<form name="a" action="svc/Thing.cfc?method=save"></form>
<form name="b"></form>
<form name="c" action="#target#"></form>
<form action="https://elsewhere.example/x.cfm"></form>
<a href="##top">top</a> <a href="mailto:x@example.com">mail</a> <a href="next.cfm?page=#p#">next</a>"""
    out = run("app/page.cfm", "<cfoutput>" + text + "</cfoutput>")
    submits = {r["subject"]["natural_key"]["name"]: r for r in relations(out, "SUBMITS_TO")}
    assert submits["a"]["object"]["kind"] == "Endpoint" and submits["a"]["object"]["natural_key"]["route"] == "/app/svc/Thing.cfc?method=save"
    assert submits["b"]["object"]["natural_key"]["name"] == "app/page.cfm" and submits["b"]["qualifiers"] == {"implicit": True}
    assert set(submits) == {"a", "b"}  # a computed action and another site are not guessed
    assert any("computed action" in m for m in messages(out))
    navs = [r["object"]["natural_key"]["name"] for r in relations(out, "NAVIGATES_TO")]
    assert navs == ["app/next.cfm"]
    assert {e["natural_key"]["name"] for e in entities(out, "Form")} == {"a", "b", "c", "form4"}


def test_application_cfc_lifecycle_and_settings():
    out = run("Application.cfc", (F20 / "base/Application.cfc").read_text())
    (program,) = entities(out, "Program")
    app = program["attributes"]["application"]
    assert app["events"] == ["onApplicationStart", "onError", "onRequestStart", "onSessionStart"]
    assert app["settings"] == {"name": "OrderDesk", "datasource": "orderdb", "sessionmanagement": True, "loginstorage": "session"}
    assert "sessiontimeout" in app["configured"] and app["mappings"] == ["/lib"]
    events = {e["natural_key"]["name"]: e["attributes"].get("lifecycle_event") for e in entities(out, "Symbol")}
    assert events["onrequeststart"] == "onRequestStart"
    assert {"cf:onApplicationStart", "cf:onRequestStart", "cf:scope.application"} <= set(constructs(out))
    assert ("Program", "datasource:orderdb") in {(r["subject"]["kind"], r["object"]["name"]) for r in relations(out, "REFERENCES_CONFIG")}


def test_login_roles_and_role_checks():
    out = run("login.cfm", (F20 / "base/login.cfm").read_text())
    assert {e["natural_key"]["name"] for e in entities(out, "Role")} == {"clerk", "manager"}
    gated = run("reports/sales.cfm", (F20 / "base/reports/sales.cfm").read_text())
    needs = {(r["subject"]["kind"], r["object"]["natural_key"]["name"], r["qualifiers"]["via"]) for r in relations(gated, "REQUIRES_ROLE")}
    assert needs == {("Program", "manager", "isuserinrole"), ("Screen", "manager", "isuserinrole")}
    script = run("x/Svc.cfc", 'component { remote function a() roles="Admin, Editor" { if (isUserInRole("auditor")) { return 1; } } }')
    assert {e["natural_key"]["name"] for e in entities(script, "Role")} == {"admin", "editor", "auditor"}


def test_scheduled_task_from_cfschedule():
    out = run("admin/schedule.cfm", (F20 / "base/admin/schedule.cfm").read_text())
    (job,) = entities(out, "Job")
    (task,) = entities(out, "Task")
    assert job["natural_key"] == {"framework": "cfschedule", "name": "default"}
    assert task["natural_key"] == {"framework": "cfschedule", "job": "default", "name": "nightly-cleanup"}
    assert task["attributes"]["url_path"] == "/jobs/nightly.cfm" and "localhost" not in json.dumps(task)
    (runs,) = relations(out, "RUNS")
    assert runs["object"]["natural_key"] == {"platform": "cfml", "name": "jobs/nightly.cfm"}


def test_orm_component_insert_and_query_of_queries():
    orm = run("model/Customer.cfc", 'component persistent="true" table="CUSTOMERS" { property name="id" fieldtype="id"; }')
    assert [e["natural_key"]["name"] for e in entities(orm, "Table")] == ["customers"]
    assert "cf:persistent-component" in constructs(orm)
    insert = run("x.cfm", '<cfinsert datasource="db" tablename="audit_trail" formfields="a,b">')
    assert [r["object"]["name"] for r in relations(insert, "WRITES_TO")] == ["audit_trail"]
    qoq = run("y.cfm", '<cfquery name="top" dbtype="query">SELECT * FROM recent WHERE total > 10</cfquery>')
    assert not relations(qoq, "READS_FROM"), "a query of queries reads a result set, not a table"


def test_components_resolve_against_every_ancestor_directory():
    out = run("a/b/page.cfm", '<cfset x = createObject("component", "lib.Util")><cfset y = new lib.Other()><cfinvoke component="lib.Util" method="run">')
    names = {r["object"]["natural_key"]["name"] for r in relations(out, "CALLS") if r["object"]["kind"] == "Program"}
    assert {"a.b.lib.util", "a.lib.util", "lib.util", "a.b.lib.other", "a.lib.other", "lib.other"} <= names
    assert all(r["object"].get("declare") is False for r in relations(out, "CALLS"))
    methods = {r["object"]["natural_key"]["program"] for r in relations(out, "CALLS") if r["object"]["kind"] == "Symbol"}
    assert methods == {"a.b.lib.util", "a.lib.util", "lib.util"}
    assert resolve_include("a/b/page.cfm", "../inc/x.cfm") == "a/inc/x.cfm" and resolve_include("a/page.cfm", "/inc/x.cfm") == "inc/x.cfm"
    assert resolve_url("a/page.cfm", "../../x.cfm") is None


# ------------------------------------------------------------------ dynamic targets and diagnostics


def test_dynamic_targets_stay_dynamic():
    text = """<cfinclude template="#page#.cfm">
<cflocation url="#session.next#">
<cfset obj = createObject("component", variables.kind)>
<cfinvoke component="#svc#" method="go">
<cfset r = evaluate("a" & b)>
<cfmodule template="#tpl#">"""
    out = run("dyn.cfm", text)
    assert not relations(out, "CALLS") and not relations(out, "NAVIGATES_TO")
    found = " | ".join(messages(out))
    for phrase in ("cfinclude of a computed template", "cflocation to a computed URL", "createObject of a computed name", "cfinvoke on a computed component", "evaluate() evaluates code", "cfmodule with a computed template"):
        assert phrase in found
    body = behaviours(out)["dyn.cfm"]["body"]
    include = next(st for st in ops(body) if st["op"] == "include")
    assert include["target"]["e"] == "template"
    redirect = next(st for st in ops(body) if st["op"] == "redirect")
    assert redirect["target"] == {"e": "var", "name": "next", "scope": "session", "at": redirect["target"]["at"]}
    assert constructs(out)["cf:evaluate"] == [False]


@pytest.mark.parametrize("sql, kept", [
    ("SELECT * FROM #tbl# WHERE id = 1", set()),
    ("SELECT o.id FROM orders_#url.region# o JOIN #tbl# t ON t.id = o.id JOIN customers c ON c.id = o.cid", {"customers"}),
    ("INSERT INTO audit_#y# (a) VALUES (1)", set()),
    ("SELECT id FROM #schema#.orders", set()),
])
def test_a_table_named_at_run_time_stays_dynamic(sql, kept):
    """`#expr#` in a table name is masked for parsing; the masked text is never declared as a table."""
    out = run("dyn.cfm", f'<cfquery name="q" datasource="db">{sql}</cfquery>')
    tables = {e["natural_key"]["name"] for e in entities(out, "Table")}
    assert tables == kept, tables
    assert not [t for t in tables if "x_" in t]
    assert any("table computed at run time" in m for m in messages(out))
    (query,) = [st for st in ops(behaviours(out)["dyn.cfm"]["body"]) if st["op"] == "query"]
    assert query["params"] and all(p["inline"] is True for p in query["params"])
    assert ":" in query["sql"]
    script = run("Dyn.cfc", f'component {{ function f() {{ return queryExecute("{sql}"); }} }}')
    assert {e["natural_key"]["name"] for e in entities(script, "Table")} == kept
    assert any("table computed at run time" in m for m in messages(script))


def test_inline_values_outside_table_names_keep_the_tables():
    out = run("q.cfm", '<cfquery name="q" datasource="db">SELECT id FROM orders WHERE region = \'#url.region#\' AND id > #minId#</cfquery>')
    assert {e["natural_key"]["name"] for e in entities(out, "Table")} == {"orders"}
    assert not any("computed at run time" in m for m in messages(out))


def test_control_names_follow_the_contract_order():
    """name, id, link:<target>, submit or button:<text>, else <type><n> among the controls that fall back."""
    text = """<cfoutput>
<a href="a.cfm">A</a>
<a href="b.cfm?id=#id#">B</a>
<a id="home" href="index.cfm">Home</a>
<form name="f">
  <input type="text" id="who">
  <input type="text">
  <input type="checkbox">
  <input type="text">
  <input type="submit" value="Go">
  <input type="button" value="Reset all">
  <button type="button"><b>Preview</b> page</button>
  <button>Label #x#</button>
  <cfinput type="submit" name="save" value="Save">
</form>
</cfoutput>"""
    out = run("app/p.cfm", text)
    names = {(e["natural_key"]["container"], e["natural_key"]["name"]) for e in entities(out, "UIControl")}
    assert names == {
        ("app/p.cfm", "link:a.cfm"), ("app/p.cfm", "link:b.cfm"), ("app/p.cfm", "home"),
        ("app/p.cfm#f", "who"), ("app/p.cfm#f", "text1"), ("app/p.cfm#f", "checkbox1"), ("app/p.cfm#f", "text2"),
        ("app/p.cfm#f", "submit"), ("app/p.cfm#f", "button:Reset all"), ("app/p.cfm#f", "button:Preview page"),
        ("app/p.cfm#f", "button1"), ("app/p.cfm#f", "save"),
    }
    # A link added before the others renames nothing.
    more = run("app/p.cfm", text.replace("<cfoutput>", '<cfoutput><a href="z.cfm">Z</a>', 1))
    before = {n for _, n in names}
    after = {e["natural_key"]["name"] for e in entities(more, "UIControl")}
    assert after == before | {"link:z.cfm"}


def control_names(out):
    return [(e["natural_key"]["container"], e["natural_key"]["name"]) for e in entities(out, "UIControl")]


def test_control_names_are_trimmed_and_empty_values_do_not_count():
    text = """<form name=" f ">
  <input name="  qty  ">
  <input name="   " id=" who ">
  <input name="" id="">
  <input type=" CHECKBOX " name=" ">
  <button type=" submit ">Go</button>
  <button>   </button>
  <button>  Save   and
     close </button>
</form>
<a href="  list.cfm?x=1  ">L</a>
<a href="   ">blank</a>"""
    out = run("app/p.cfm", text)
    assert control_names(out) == [
        ("app/p.cfm#f", "qty"), ("app/p.cfm#f", "who"), ("app/p.cfm#f", "text1"), ("app/p.cfm#f", "checkbox1"),
        ("app/p.cfm#f", "submit"), ("app/p.cfm#f", "button1"), ("app/p.cfm#f", "button:Save and close"),
        ("app/p.cfm", "link:list.cfm"), ("app/p.cfm", "link1"),
    ]


def test_fallback_names_count_only_fallback_controls_per_container():
    """`<type><n>`: n counts, per container and type, only the controls that fall back to it."""
    text = """<input type="text"><input type="text" name="a"><input type="text">
<form name="f"><input type="text"><select></select><input type="text" id="b"><select></select><textarea></textarea><input></form>
<form name="g"><input type="text"></form>
<a href="##x">x</a><a href="#url#">dyn</a>"""
    out = run("app/p.cfm", "<cfoutput>" + text + "</cfoutput>")
    assert control_names(out) == [
        ("app/p.cfm", "text1"), ("app/p.cfm", "a"), ("app/p.cfm", "text2"),
        ("app/p.cfm#f", "text1"), ("app/p.cfm#f", "select1"), ("app/p.cfm#f", "b"), ("app/p.cfm#f", "select2"),
        ("app/p.cfm#f", "textarea1"), ("app/p.cfm#f", "text2"),
        ("app/p.cfm#g", "text1"),
        ("app/p.cfm", "link1"), ("app/p.cfm", "link2"),  # an anchor on the page and a computed target fall back
    ]
    # Naming a fallback control renames nothing else.
    named = run("app/p.cfm", "<cfoutput>" + text.replace('<input type="text"><input type="text" name="a">', '<input type="text" name="z"><input type="text" name="a">', 1) + "</cfoutput>")
    assert control_names(named)[:3] == [("app/p.cfm", "z"), ("app/p.cfm", "a"), ("app/p.cfm", "text1")]
    assert control_names(named)[3:] == control_names(out)[3:]


def test_repeated_control_names_in_one_container_get_a_suffix():
    text = """<form name="f">
  <input type="submit" value="Save"><input type="submit" value="Save and close"><button type="submit">Again</button>
  <input type="button" value="Undo"><button type="button">Undo</button>
  <input name="q"><input name="q">
  <input name="text1"><input type="text">
</form>
<form name="g"><input type="submit"></form>
<a href="list.cfm">All</a> <a href="list.cfm?status=open">Open</a> <a href="list.cfm##top">Top</a>
<a href="https://a.example.com/x">A</a> <a href="https://a.example.com/y?q=1">A again</a>
<a href="mailto:someone@example.com">mail</a>"""
    out = run("app/p.cfm", "<cfoutput>" + text + "</cfoutput>")
    assert control_names(out) == [
        ("app/p.cfm#f", "submit"), ("app/p.cfm#f", "submit~2"), ("app/p.cfm#f", "submit~3"),
        ("app/p.cfm#f", "button:Undo"), ("app/p.cfm#f", "button:Undo~2"),
        ("app/p.cfm#f", "q"), ("app/p.cfm#f", "q~2"),
        ("app/p.cfm#f", "text1"), ("app/p.cfm#f", "text1~2"),
        ("app/p.cfm#g", "submit"),  # another container: no suffix
        ("app/p.cfm", "link:list.cfm"), ("app/p.cfm", "link:list.cfm~2"), ("app/p.cfm", "link:list.cfm~3"),
        ("app/p.cfm", "link:https://a.example.com"), ("app/p.cfm", "link:https://a.example.com~2"),
        ("app/p.cfm", "link:mailto:"),
    ]
    assert len(set(control_names(out))) == len(control_names(out))  # no two controls share a key
    # Every link is a control; only the ones that reach a page in the application navigate.
    navs = [(r["subject"]["natural_key"]["name"], r["object"]["natural_key"]["name"]) for r in relations(out, "NAVIGATES_TO")]
    assert navs == [("link:list.cfm", "app/list.cfm"), ("link:list.cfm~2", "app/list.cfm"), ("link:list.cfm~3", "app/list.cfm")]
    assert constructs(out)["cf:html-link"] == [True] * 6


def test_a_link_to_another_host_keeps_only_scheme_and_host():
    text = """<a href="https://user:pw@help.example.com:8443/a/b?ticket=1##x">h</a>
<a href="//cdn.example.com/lib.js">c</a>"""
    out = run("p.cfm", "<cfoutput>" + text + "</cfoutput>")
    assert [n for _, n in control_names(out)] == ["link:https://help.example.com:8443", "link://cdn.example.com"]
    blob = json.dumps([o.payload for o in out.observations])
    assert "pw@" not in blob and "user:" not in blob and "ticket=1" not in blob and "/a/b" not in json.dumps(control_names(out))


def test_button_text_is_literal_and_at_most_40_characters():
    text = """<form name="f">
<button type="button">Label #x#</button>
<button type="button"><cfif a>Yes<cfelse>No</cfif></button>
<button type="button">#### Tag</button>
<button type="button">{}</button>
<input type="button" value="#label#">
<input type="reset" value="Clear">
<input type="image" src="go.png">
</form>"""
    out = run("p.cfm", "<cfoutput>" + text.format("x" * 41) + "</cfoutput>")
    assert [n for _, n in control_names(out)] == ["button1", "button2", "button:## Tag", "button3", "button4", "button:Clear", "image1"]
    static = run("q.cfm", '<form name="f"><button type="button">Colour #fff</button></form>')
    assert [n for _, n in control_names(static)] == ["button:Colour #fff"]  # outside cfoutput a hash is text


def test_url_handling_raises_no_deprecation_warning():
    """`re.split` takes `maxsplit` by keyword: passing it by position is deprecated."""
    import warnings

    text = '<cflocation url="a.cfm?x=1"><cfoutput><a href="b.cfm?id=#id#">b</a><a href="c.cfm?y=2">c</a></cfoutput>'
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        out = run("p.cfm", text)
        assert resolve_url("p.cfm", "d.cfm?z=3") == "d.cfm"
    assert [n for _, n in control_names(out)] == ["link:b.cfm", "link:c.cfm"]


def test_functions_belong_to_their_program_and_units_say_their_kind():
    out = run("services/OrderService.cfc", (F20 / "base/services/OrderService.cfc").read_text())
    contains = {(r["subject"]["kind"], r["subject"]["natural_key"]["name"], r["object"]["natural_key"]["name"]) for r in relations(out, "CONTAINS")}
    assert contains == {("Program", "services.orderservice", n) for n in ("getorder", "saveorder", "notify")}
    assert {u["kind"] for u in behaviours(out).values()} == {"function"}
    page = run("orders/list.cfm", (F20 / "base/orders/list.cfm").read_text())
    assert behaviours(page)["list.cfm"]["kind"] == "page"
    pseudo = run("x/C.cfc", 'component { variables.ready = true; function go() { return 1; } }')
    units = behaviours(pseudo)
    assert units["go"]["kind"] == "function" and units["x.c"]["kind"] == "open_code"
    assert all(validate_unit(u) == [] for u in units.values())


def test_server_side_redirects_navigate_from_the_code_that_runs_them():
    save = run("orders/save.cfm", (F20 / "base/orders/save.cfm").read_text())
    assert not entities(save, "Screen")
    navs = {(r["subject"]["kind"], r["subject"]["natural_key"]["name"], r["object"]["natural_key"]["name"]) for r in relations(save, "NAVIGATES_TO")}
    assert navs == {("Program", "orders/save.cfm", n) for n in ("orders/edit.cfm", "orders/list.cfm", "login.cfm")}  # session.returnTo is not guessed
    cfc = run("svc/Auth.cfc", 'component { function bounce() { location(url="../login.cfm", addtoken=false); } function away() { location(url=target); } }')
    (nav,) = relations(cfc, "NAVIGATES_TO")
    assert nav["subject"]["kind"] == "Symbol" and nav["subject"]["natural_key"]["name"] == "bounce"
    assert nav["object"]["natural_key"] == {"platform": "cfml", "name": "login.cfm"}
    tag = run("p.cfm", '<cffunction name="out"><cflocation url="bye.cfm"></cffunction><p>hello</p>')
    subjects = {(r["subject"]["kind"], r["object"]["natural_key"]["name"]) for r in relations(tag, "NAVIGATES_TO")}
    assert subjects == {("Symbol", "bye.cfm")}
    assert entities(tag, "Screen")  # the page renders, but the redirect runs only when the function is called


def test_unreadable_script_is_kept_as_a_platform_node():
    out = run("bad.cfc", "component { function ok() { return 1; } function broken() { x = ; y = 2; } }")
    assert "cf:unparsed" in constructs(out) and constructs(out)["cf:unparsed"] == [False]
    assert any("unparsed" in m for m in messages(out))
    units = behaviours(out)
    assert set(units) == {"ok", "broken"} and validate_unit(units["broken"]) == []
    assert any(st["op"] == "assign" for st in units["broken"]["body"])  # parsing resumed after the bad statement


def test_unknown_and_unmapped_tags_are_counted_not_modelled():
    out = run("x.cfm", '<cfgrid name="g"></cfgrid><cffrobnicate a="1"><cfthread name="t"><cfset x = 1></cfthread>')
    c = constructs(out)
    assert c["cf:cfgrid"] == [False] and c["cf:unknown-tag"] == [False] and c["cf:cfthread"] == [True]
    body = behaviours(out)["x.cfm"]["body"]
    assert [st.get("construct") for st in body if st["op"] == "platform"] == ["cf:cfgrid", "cf:unknown-tag", "cf:cfthread"]


# ------------------------------------------------------------------ behaviour lowering


def test_tag_page_lowering():
    text = """<cfparam name="url.page" default="1" type="numeric">
<cfquery name="q" datasource="db">
  SELECT id FROM orders WHERE status = <cfqueryparam value="#url.status#" cfsqltype="cf_sql_varchar"> AND id > #minId#
</cfquery>
<cfloop query="q"><cfif q.id GT 10><cfbreak><cfelseif q.id EQ 5><cfcontinue><cfelse><cfset total = total + q.id></cfif></cfloop>
<cfloop from="1" to="#n#" index="i" step="2"><cfset x[i] = i * 2></cfloop>
<cfswitch expression="#mode#"><cfcase value="a,b"><cfset m = 1></cfcase><cfdefaultcase><cfset m = 0></cfdefaultcase></cfswitch>
<cftry><cfset risky()><cfcatch type="any"><cflog text="failed"></cfcatch><cffinally><cfset done = true></cffinally></cftry>
<cflock name="counter" type="exclusive" timeout="5"><cfset application.count++></cflock>
<cfoutput>Total: #numberFormat(total)#</cfoutput>"""
    out = run("page.cfm", text)
    body = behaviours(out)["page.cfm"]["body"]
    assert validate_unit(behaviours(out)["page.cfm"]) == []
    kinds = [st["op"] for st in body]
    assert kinds == ["declare", "query", "loop", "loop", "switch", "try", "lock", "output"]
    query = body[1]
    assert query["sql"] == "SELECT id FROM orders WHERE status = :status AND id > :minId" and query["datasource"] == "db"
    bound, inline = query["params"]
    assert bound["type"] == "string" and "inline" not in bound
    assert inline["name"] == "minId" and inline["inline"] is True  # #minId# is pasted into the SQL text, not bound
    assert body[0]["scope"] == "url" and body[0]["name"] == "page"
    inner = {st["op"] for st in ops(body)}
    assert {"if", "break", "continue", "assign", "output"} <= inner
    assert body[3]["kind"] == "range"
    assert [t["op"] for t in body[6]["body"]] == ["assign"]
    assert [r["object"]["name"] for r in relations(out, "READS_FROM")] == ["orders"]


def test_script_lowering():
    text = """component {
    public function run(required array items, string mode = "fast") output=false {
        var total = 0;
        var seen = {};
        for (var i = 1; i <= arrayLen(arguments.items); i++) { total += arguments.items[i]; }
        for (var item in items) { seen[item] = true; }
        while (total > 100) { total = total / 2; }
        do { total--; } while (total > 50);
        switch (mode) { case "fast": total = total * 2; break; default: total = 0; }
        try { risky(); } catch (any e) { writeLog(text = e.message); rethrow; } finally { cleanup(); }
        var double = function(n) { return n * 2; };
        var triple = (n) => n * 3;
        local.label = mode == "fast" ? "quick" : "slow";
        local.name = session.user ?: "guest";
        lock name="x" type="exclusive" timeout="1" { application.hits++; }
        transaction { queryExecute("UPDATE stats SET n = n + 1", {}, {datasource: "db"}); }
        writeOutput("done #total#");
        return double(total);
    }
    private function risky() { throw(type = "Oops", message = "no"); }
    private function cleanup() {}
}"""
    out = run("svc/Calc.cfc", text)
    assert not [m for m in messages(out) if "unparsed" in m], messages(out)
    unit = behaviours(out)["run"]
    assert validate_unit(unit) == []
    assert [p["name"] for p in unit["params"]] == ["items", "mode"]
    found = {st["op"] for st in ops(unit["body"])}
    assert {"declare", "loop", "switch", "try", "throw", "lock", "transaction", "query", "output", "return", "assign", "break"} <= found
    loops = sorted(st["kind"] for st in ops(unit["body"]) if st["op"] == "loop")
    assert loops == ["each", "for", "while", "while"]  # do-while is a while loop tested after the body
    exprs = {n.get("e") for n in walk(unit["body"])}
    assert {"lambda", "cond", "binop", "call", "member", "index", "template", "struct"} <= exprs
    scopes = {n.get("scope") for n in walk(unit["body"]) if n.get("e") == "var"}
    assert {"local", "arguments", "session", "application"} <= scopes
    calls = {(r["subject"]["name"], r["object"]["name"]) for r in relations(out, "CALLS")}
    assert {("run", "risky"), ("run", "cleanup")} <= calls
    assert [r["object"]["name"] for r in relations(out, "WRITES_TO") if r["subject"]["kind"] == "Symbol"] == ["stats"]


def test_cfscript_inside_a_page_and_mixed_tags():
    text = """<cfscript>
    items = [1, 2, 3];
    total = arrayReduce(items, function(acc, n) { return acc + n; }, 0);
    if (total > 5) { location(url = "big.cfm", addToken = false); }
</cfscript>
<cfoutput><p>#total#</p></cfoutput>"""
    out = run("mix.cfm", text)
    body = behaviours(out)["mix.cfm"]["body"]
    assert [st["op"] for st in body][:3] == ["assign", "assign", "if"]
    assert any(st["op"] == "redirect" for st in ops(body))
    (nav,) = relations(out, "NAVIGATES_TO")
    assert nav["object"]["natural_key"]["name"] == "big.cfm" and nav["qualifiers"]["via"] == "cflocation"


def test_unscoped_variables_resolve_to_their_scope():
    text = """<cffunction name="f"><cfargument name="a"><cfset var b = a + 1><cfset c = b><cfreturn c></cffunction>"""
    out = run("s.cfc", "<cfcomponent>" + text + "</cfcomponent>")
    unit = behaviours(out)["f"]
    assigns = [st for st in ops(unit["body"]) if st["op"] in ("assign", "declare")]
    scopes = {(st["target"]["name"] if "target" in st else st["name"]): (st["target"].get("scope") if "target" in st else st.get("scope")) for st in assigns}
    assert scopes == {"b": "local", "c": "variables"}
    reads = {n["name"]: n.get("scope") for n in walk(unit["body"]) if n.get("e") == "var"}
    assert reads["a"] == "arguments"


# ------------------------------------------------------------------ markup


def test_markup_comments_strings_and_unclosed_tags():
    text = """<!--- outer <!--- inner ---> <cfset hidden = 1> --->
<cfset s = "say ""hi"" ## #name#">
<cfif a><cfinvoke component="x" method="y"><p>x</p></cfif>
<!-- <form name="commented"></form> -->
<script>var f = "<form>";</script>
<form name="real"><input name="q"></form>"""
    doc = parse_markup(text)
    tags = []

    def collect(n):
        for c in n.children:
            if c.kind == "tag":
                tags.append(c.name)
            collect(c)

    collect(doc.root)
    assert tags == ["cfset", "cfif", "cfinvoke"]
    forms = [e.node.literal("name") for e in doc.ui if e.kind == "form_open"]
    assert forms == ["real"]
    out = run("m.cfm", text)
    assigns = [st for st in ops(behaviours(out)["m.cfm"]["body"]) if st["op"] == "assign"]
    assert [a["target"]["name"] for a in assigns] == ["s"]  # the cfset inside the comment is not code
    value = assigns[0]["value"]
    assert value["e"] == "template" and value["parts"][0] == 'say "hi" # '


def test_byte_spans_with_non_ascii_text():
    text = "<p>café – ünïcode</p><cfquery datasource=\"db\">SELECT * FROM straße_t</cfquery>"
    data = text.encode("utf-8")
    out = SOURCE.extract("u.cfm", data, BLOB)
    (read,) = relations(out, "READS_FROM")
    ev = next(o for o in out.observations if o.kind == "relation" and o.payload["predicate"] == "READS_FROM").evidence
    assert read["object"]["name"] == "straße_t"
    assert data[ev.span.start_byte: ev.span.end_byte].decode() == "straße_t"  # character offsets mapped to bytes


def test_deep_nesting_does_not_crash():
    out = run("deep.cfm", "<cfset x = " + "(" * 400 + "1" + ")" * 400 + ">")
    assert behaviours(out)["deep.cfm"]
    out = run("deep2.cfm", "<cfif a>" * 300 + "x" + "</cfif>" * 300)
    assert validate_unit(behaviours(out)["deep2.cfm"]) == []


def test_parsing_is_linear_enough_for_large_files():
    import time

    block = '<cfset a = b + 1><cfif a GT 2><cfoutput>#a#</cfoutput></cfif><a href="x.cfm">x</a>\n'
    small, large = block * 500, block * 4000
    t0 = time.perf_counter()
    run("s.cfm", small)
    t1 = time.perf_counter()
    run("l.cfm", large)
    t2 = time.perf_counter()
    assert (t2 - t1) < max(0.5, (t1 - t0) * 8 * 4), "eight times the input must not take more than about eight times as long"


# ------------------------------------------------------------------ Administrator exports and secrets


def test_admin_exports_give_names_driver_and_url_path_only():
    ds = ADMIN.extract(".rkp/inputs/cfml/neo-datasource.xml", (F20 / "base/.rkp/inputs/cfml/neo-datasource.xml").read_bytes(), BLOB)
    cron = ADMIN.extract(".rkp/inputs/cfml/neo-cron.xml", (F20 / "base/.rkp/inputs/cfml/neo-cron.xml").read_bytes(), BLOB)
    names = {e["natural_key"]["name"]: e["attributes"] for e in entities(ds, "ConfigKey")}
    assert set(names) == {"datasource:orderdb", "datasource:reportdb"}
    assert names["datasource:orderdb"]["driver"] == "MSSQLServer" and names["datasource:orderdb"]["platform_kind"] == "cf:Datasource"
    (task,) = entities(cron, "Task")
    assert task["natural_key"] == {"framework": "cfschedule", "job": "reports", "name": "weekly-sales"}
    assert task["attributes"]["url_path"] == "/reports/weekly.cfm"
    assert relations(cron, "RUNS")[0]["object"]["natural_key"]["name"] == "reports/weekly.cfm"
    assert all(str(o.evidence_class) == "E1" for o in ds.observations + cron.observations if o.kind in ("entity", "relation"))
    blob = json.dumps([o.payload for o in ds.observations + cron.observations])
    for secret in ("FIXTURE-NOT-A-SECRET", "fixture-db-host", "fixture-report-host", "fixture-intranet-host", "fixture_app_user", "fixture_scheduler", "jdbc:", "key=", "8443"):
        assert secret not in blob


def test_admin_refuses_doctype_and_ignores_other_files():
    hostile = b"<?xml version='1.0'?><!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/passwd'>]><wddxPacket><data/></wddxPacket>"
    out = ADMIN.extract(".rkp/inputs/cfml/neo-datasource.xml", hostile, BLOB)
    assert not out.observations and out.parsed is False and "DOCTYPE" in out.diagnostics[0].message
    assert not ADMIN.matches("config/neo-datasource.xml") and not ADMIN.matches(".rkp/inputs/cfml/neo-mail.xml")
    assert ADMIN.matches(".rkp/inputs/cfml/neo-cron.xml")


def unit_strings(out):
    """Every literal string and template part in every behaviour unit."""
    found = []
    for o in out.observations:
        if o.kind == "behavior":
            for n in walk(o.payload):
                if n.get("e") == "lit" and isinstance(n.get("value"), str):
                    found.append(n["value"])
                elif n.get("e") == "template":
                    found += [p for p in n["parts"] if isinstance(p, str)]
    return found


def redirects(out):
    return [st["target"] for o in out.observations if o.kind == "behavior" for st in ops(o.payload["body"]) if st["op"] == "redirect"]


def test_redirect_urls_keep_parameter_names_not_values():
    out = run("orders/x.cfm", '<cflocation url="login.cfm?token=abc123" addtoken="no">')
    (target,) = redirects(out)
    assert target["e"] == "lit" and target["value"] == "login.cfm?token="
    script = run("svc/Auth.cfc", 'component { function bounce() { location("../home.cfm?sid=987"); } }')
    (target,) = redirects(script)
    assert target["value"] == "../home.cfm?sid="
    assert [r["object"]["natural_key"]["name"] for r in relations(script, "NAVIGATES_TO")] == ["home.cfm"]
    named = run("p.cfm", '<cfscript>location(url = "a.cfm?x=1&y=2##frag", addToken = false);</cfscript>')
    assert redirects(named)[0]["value"] == "a.cfm?x=&y="
    for o in (out, script, named):
        blob = json.dumps([x.payload for x in o.observations])
        assert "abc123" not in blob and "987" not in blob and "frag" not in blob


def test_a_url_template_keeps_its_structure_and_expressions_but_no_literal_values():
    out = run("orders/save.cfm", '<cflocation url="edit.cfm?id=#form.orderId#&error=1&mode=fixture-mode##top#anchor#" addtoken="no">')
    (target,) = redirects(out)
    assert target["e"] == "template"
    parts = target["parts"]
    assert parts[0] == "edit.cfm?id=" and parts[1]["e"] == "var" and parts[1]["name"] == "orderId" and parts[2] == "&error=&mode="
    assert len(parts) == 3  # the fragment, and the expression in it, are gone
    assert "fixture-mode" not in json.dumps([o.payload for o in out.observations])
    assert [r["object"]["natural_key"]["name"] for r in relations(out, "NAVIGATES_TO")] == ["orders/edit.cfm"]
    based = run("p.cfm", '<cfset next = "#application.base#/login.cfm?token=fixture-token-9">')
    assert "fixture-token-9" not in json.dumps(unit_strings(based)) and "/login.cfm?token=" in unit_strings(based)


def test_cfhttp_urls_and_parameter_values_are_not_copied():
    secret = "https://svc-user:fixture-pass@api.example.com/oauth/token?client_id=desk&client_secret=fixture-client-secret##frag"
    tag = run("p.cfm", f'''<cfhttp url="{secret}" method="post" result="r">
  <cfhttpparam type="header" name="Authorization" value="Bearer fixture-bearer">
  <cfhttpparam type="url" name="client_secret" value="fixture-param-secret">
  <cfhttpparam type="formfield" name="who" value="#form.who#">
</cfhttp>''')
    script = run("Svc.cfc", 'component { function f() { cfhttp(url = "' + secret + '", method = "get", result = "r"); return r; } }')
    for out in (tag, script):
        (call,) = [n for o in out.observations if o.kind == "behavior" for n in walk(o.payload) if n.get("e") == "call" and n["callee"].get("name") == "cfhttp"]
        assert call["named"]["url"]["value"] == "https://api.example.com/oauth/token?client_id=&client_secret="
        blob = json.dumps([o.payload for o in out.observations])
        for value in ("svc-user", "fixture-pass", "=desk", "fixture-client-secret", "frag", "fixture-bearer", "fixture-param-secret"):
            assert value not in blob, value
    params = [n["fields"] for o in tag.observations if o.kind == "behavior" for n in walk(o.payload) if n.get("e") == "struct" and "type" in n.get("fields", {})]
    assert [sorted(p) for p in params] == [["name", "type"], ["name", "type"], ["name", "type", "value"]]  # a computed value stays


def test_urls_in_links_form_actions_and_literals_are_scrubbed():
    text = """<cfoutput>
<a href="edit.cfm?id=#q.id#&token=fixture-link-token##sec">#q.id#</a>
<a href='https://fixture-user@x.example.com/p?k=fixture-ext-key'>x</a>
<form name="f" action="save.cfm?step=fixture-step&id=#q.id#" method="post"></form>
</cfoutput>
<cfset api = "https://api.example.com/v1/items?apikey=fixture-api-key">
<cfset page = "list.cfm?filter=fixture-filter">
<cfset mail = "mailto:fixture-person@example.com">
<cfset html = '<a href="next.cfm?sid=fixture-sid">next</a>'>
<cfset colour = "##ff0000">
<cfset words = "Are you sure? Yes=1">
<cfscript>writeOutput('<form action="go.cfm?x=fixture-script-value"></form>');</cfscript>"""
    out = run("orders/p.cfm", text)
    strings = unit_strings(out)
    blob = json.dumps([o.payload for o in out.observations]) + json.dumps(messages(out))
    for value in ("fixture-link-token", "sec\"", "fixture-user", "fixture-ext-key", "fixture-step", "fixture-api-key", "fixture-filter",
                  "fixture-person", "fixture-sid", "fixture-script-value"):
        assert value not in blob, value
    for kept in ("https://api.example.com/v1/items?apikey=", "list.cfm?filter=", "mailto:", '<a href="next.cfm?sid=">next</a>', "#ff0000",
                 "Are you sure? Yes=1", '<form action="go.cfm?x="></form>'):
        assert kept in strings, kept
    link = next(s for s in strings if 'href="edit.cfm' in s)
    assert link.endswith('href="edit.cfm?id=')
    assert any(s.startswith('&token="') for s in strings)  # the value and fragment are gone, the expression stays
    (submit,) = relations(out, "SUBMITS_TO")
    assert submit["object"]["natural_key"]["name"] == "orders/save.cfm"


def test_no_planted_value_in_f20_reaches_any_output():
    """The F20 pages and components carry values in their URLs (`f20-planted-...`), and its
    Administrator exports carry connection details: none of them is in any output."""
    planted = ("f20-planted", "FIXTURE-NOT-A-SECRET", "fixture-db-host", "fixture-report-host", "fixture-intranet-host", "fixture_app_user", "fixture_scheduler")
    layers = [F20 / "base", *(p for p in sorted((F20 / "commits").iterdir()) if p.is_dir())]
    seen = 0
    for layer in layers:
        for path in sorted(p for p in layer.rglob("*") if p.is_file()):
            rel = str(path.relative_to(layer))
            for extractor in (SOURCE, ADMIN):
                if not extractor.matches(rel):
                    continue
                out = extractor.extract(rel, path.read_bytes(), BLOB)
                blob = json.dumps([o.payload for o in out.observations]) + json.dumps(messages(out))
                for value in planted:
                    assert value not in blob, (rel, value)
                seen += 1
    assert seen >= 15
    assert "f20-planted" in (F20 / "base/services/ReportService.cfc").read_text()  # the values are there to be left out


def test_credentials_and_addresses_in_code_are_not_copied():
    text = """<cfloginuser name="#u#" password="hunter2-literal" roles="clerk">
<cfhttp url="https://api.example.com/x" method="get" username="svc" password="s3cret-literal" result="r">
<cfmail to="boss@example.com" from="app@example.com" subject="Hi">Body</cfmail>
<cfquery datasource="db" username="dbuser" password="dbpass-literal">SELECT 1 FROM t</cfquery>"""
    out = run("creds.cfm", text)
    blob = json.dumps([o.payload for o in out.observations])
    for value in ("hunter2-literal", "s3cret-literal", "dbpass-literal", "boss@example.com", "app@example.com", "dbuser"):
        assert value not in blob
