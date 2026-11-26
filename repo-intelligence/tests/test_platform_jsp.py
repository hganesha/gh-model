"""The JSP platform pack (repo_intel/platforms/jsp): pages, tag files and fragments read by
`jsp.source`, and the servlet deployment descriptor read by `jsp.webxml`."""

import hashlib
import json
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import validate_unit
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.platforms import packs, validate_pack
from repo_intel.platforms.jsp import PACK, JspSource, JspWebXml
from repo_intel.platforms.jsp import el as EL
from repo_intel.platforms.jsp.source import url_of, webroot
from repo_intel.platforms.jsp.webxml import covers

FIXTURES = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures"
F25 = FIXTURES / "F25-jsp-app" / "base"
WEBAPP = "src/main/webapp/"


def run(text: str | bytes, path: str = WEBAPP + "page.jsp", extractor=None):
    data = text if isinstance(text, bytes) else text.encode()
    return (extractor or JspSource()).extract(path, data, hashlib.sha256(data).hexdigest())


def fixture(path: str, extractor=None):
    return run((F25 / path).read_bytes(), path, extractor)


def ref(x) -> tuple:
    return x if isinstance(x, str) else (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def key(kind: str, **nk) -> tuple:
    return (kind, json.dumps(nk, sort_keys=True))


def entities(out) -> dict[tuple, dict]:
    found: dict[tuple, dict] = {}
    for o in out.observations:
        if o.kind == "entity":
            found.setdefault(ref(o.payload), {}).update(o.payload["attributes"])
    return found


def relations(out) -> set[tuple]:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])) for o in out.observations if o.kind == "relation"}


def constructs(out) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = {}
    for o in out.observations:
        if o.kind == "construct":
            k = (o.payload["construct"], o.payload["modelled"])
            counts[k] = counts.get(k, 0) + 1
    return counts


def messages(out) -> list[str]:
    return [d.message for d in out.diagnostics]


def prog(name: str) -> tuple:
    return key("Program", platform="jsp", name=WEBAPP + name)


def screen(name: str) -> tuple:
    return key("Screen", platform="jsp", name=WEBAPP + name)


def endpoint(route: str) -> tuple:
    return key("Endpoint", component_root="@unit", protocol="http", method="ANY", route=route)


def role(name: str) -> tuple:
    return key("Role", realm="jsp", name=name)


PAGES = sorted(str(p.relative_to(F25)) for p in F25.rglob("*") if p.suffix in (".jsp", ".jspf", ".tag", ".jspx"))


# ---------------------------------------------------------------------- the pack


def test_pack_is_registered_and_valid():
    assert packs()["jsp"] is PACK
    assert validate_pack(PACK) == []
    assert {e.id for e in PACK.extractors} == {"jsp.source", "jsp.webxml"}
    assert len(PACK.constructs) >= 60


def test_extractors_claim_their_files():
    src, web = JspSource(), JspWebXml()
    for path in ("a/b.jsp", "a/b.jspx", "a/b.jspf", "WEB-INF/tags/money.tag"):
        assert src.matches(path), path
    assert not src.matches("a/b.java") and not src.matches("a/b.html")
    assert web.matches(WEBAPP + "WEB-INF/web.xml")
    assert not web.matches("conf/web.xml.bak") and not web.matches("pom.xml")


def test_inventory_language_of_jsp_files():
    from repo_intel.extractors import detect_language

    for path in ("a/b.jsp", "a/b.jspx", "a/b.jspf", "WEB-INF/tags/money.tag", "META-INF/tags/x.TAGX"):
        assert detect_language(path) == "jsp", path


def test_tag_files_are_read_only_from_a_tag_directory():
    src = JspSource()
    for path in ("WEB-INF/tags/money.tag", WEBAPP + "WEB-INF/tags/forms/field.tagx", "lib/META-INF/tags/util.tag", "app/web-inf/tags/a.TAG"):
        assert src.matches(path), path
    # Riot.js components and other tools' .tag files are not JSP
    for path in ("src/components/todo.tag", "tags/app.tag", "WEB-INF/money.tag", "src/WEB-INF-tags/x.tag", "WEB-INF/tagsx/y.tagx", "MY-WEB-INF/tags/z.tag"):
        assert not src.matches(path), path


def test_the_pack_imports_extractors_only_inside_functions():
    import ast

    for p in (Path(__file__).resolve().parents[1] / "repo_intel" / "platforms" / "jsp").glob("*.py"):
        tree = ast.parse(p.read_text())
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("repo_intel.extractors"), p.name
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("repo_intel.extractors") for a in node.names), p.name


@pytest.mark.parametrize("path", PAGES)
def test_fixture_pages_parse_into_valid_units_relations_and_known_constructs(path):
    out = fixture(path)
    assert out.parsed
    known = {c.id for c in PACK.constructs}
    assert {cid for cid, _ in constructs(out)} <= known
    for o in out.observations:
        if o.kind == "behavior":
            assert validate_unit(o.payload) == [], path
        if o.kind == "relation" and isinstance(o.payload["subject"], dict) and isinstance(o.payload["object"], dict):
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p


# ---------------------------------------------------------------------- pages


def test_webroot_and_urls():
    assert webroot(WEBAPP + "orders/list.jsp") == WEBAPP
    assert url_of(WEBAPP + "orders/list.jsp") == "/orders/list.jsp"
    assert url_of(WEBAPP + "WEB-INF/tags/money.tag") is None
    assert url_of(WEBAPP + "WEB-INF/views/home.jsp") is None


def test_page_program_screen_and_endpoint():
    out = fixture(WEBAPP + "index.jsp")
    ents, rels = entities(out), relations(out)
    assert ents[prog("index.jsp")]["platform_kind"] == "jsp:Page"
    assert ents[screen("index.jsp")]["title"] == "Order desk"
    assert (prog("index.jsp"), "EXPOSES", endpoint("/index.jsp")) in rels
    assert (prog("index.jsp"), "RENDERS", screen("index.jsp")) in rels
    assert (prog("index.jsp"), "CALLS", prog("common/header.jspf")) in rels


def test_spring_form_names_controls_by_path_and_submits_to_the_relative_action():
    out = fixture(WEBAPP + "orders/edit.jsp")
    ents, rels = entities(out), relations(out)
    form = key("Form", screen=WEBAPP + "orders/edit.jsp", name="orderForm")
    assert ents[form]["kind"] == "spring" and ents[form]["model"] == "order"
    container = WEBAPP + "orders/edit.jsp#orderForm"
    for name in ("id", "customer", "quantity", "status", "notes"):
        assert (form, "CONTAINS", key("UIControl", container=container, name=name)) in rels, name
    assert ents[key("UIControl", container=container, name="customer")]["maxlength"] == "60"
    assert (form, "SUBMITS_TO", endpoint("/orders/save")) in rels
    # sec:authorize hasRole('admin') guards the purge link: the screen requires the role
    assert (screen("orders/edit.jsp"), "REQUIRES_ROLE", role("admin")) in rels


def test_struts_form_is_modelled_but_its_action_is_not_followed():
    out = fixture(WEBAPP + "admin/purge.jsp")
    ents = entities(out)
    form = key("Form", screen=WEBAPP + "admin/purge.jsp", name="form1")
    assert ents[form]["kind"] == "struts" and ents[form]["struts_action"] == "/admin/purge"
    assert not any(p == "SUBMITS_TO" for s, p, _ in relations(out) if s == form)
    assert any("struts-config.xml" in m for m in messages(out))
    assert (screen("admin/purge.jsp"), "REQUIRES_ROLE", role("admin")) in relations(out)


def test_computed_include_and_redirect_stay_dynamic_with_a_diagnostic():
    out = fixture(WEBAPP + "reports/sales.jsp")
    msgs = messages(out)
    assert "jsp:include of a computed page: target not resolved" in msgs
    assert "sendRedirect to a computed URL: target not resolved" in msgs
    rels = relations(out)
    assert not any(p in ("CALLS", "NAVIGATES_TO") and "param.extra" in o[1] for _, p, o in rels)
    assert (prog("reports/sales.jsp"), "REQUIRES_ROLE", role("manager")) in rels
    assert (prog("reports/sales.jsp"), "READS_FROM", key("Table", datastore="default", name="sales_daily")) in rels


def test_literal_redirect_forward_and_include_are_followed():
    text = (
        '<%@ include file="/common/header.jspf" %>\n'
        '<jsp:forward page="done.jsp"/>\n'
        '<% response.sendRedirect("login.jsp"); %>\n'
        '<c:redirect url="/orders/list.jsp"/>\n'
    )
    rels = relations(run(text, WEBAPP + "orders/x.jsp"))
    me = prog("orders/x.jsp")
    assert (me, "CALLS", prog("common/header.jspf")) in rels
    targets = {o for s, p, o in rels if p == "NAVIGATES_TO"}
    assert prog("orders/done.jsp") in targets and prog("orders/login.jsp") in targets and prog("orders/list.jsp") in targets


def test_role_checks_in_el_scriptlets_and_tags():
    text = (
        '<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>\n'
        '<%@ taglib prefix="logic" uri="http://struts.apache.org/tags-logic" %>\n'
        "<c:if test=\"${pageContext.request.isUserInRole('auditor')}\"><a href=\"audit.jsp\">Audit</a></c:if>\n"
        '<logic:present role="supervisor"><p>x</p></logic:present>\n'
        '<% if (request.isUserInRole("clerk")) { %><p>y</p><% } %>\n'
    )
    rels = relations(run(text))
    required = {o for s, p, o in rels if p == "REQUIRES_ROLE"}
    assert {role("auditor"), role("supervisor"), role("clerk")} <= required


def test_el_parser_and_role_checks():
    node, ok = EL.parse("pageContext.request.isUserInRole('admin') and not empty param.q")
    assert ok
    assert EL.role_checks(node) == ["admin"]
    node, ok = EL.parse("pageContext.request.isUserInRole(param.role)")
    assert EL.role_checks(node) == []
    _, ok = EL.parse("a ? : b (")
    assert not ok


def test_tag_file_is_a_program_with_its_attributes_and_no_endpoint():
    out = fixture(WEBAPP + "WEB-INF/tags/money.tag")
    ents = entities(out)
    tag = ents[prog("WEB-INF/tags/money.tag")]
    assert tag["kind"] == "tag" and tag["platform_kind"] == "jsp:TagFile" and tag["tag_attributes"] == ["amount"]
    assert not any(k[0] == "Endpoint" for k in ents)


def test_custom_tag_call_reaches_the_tag_file():
    rels = relations(fixture(WEBAPP + "orders/list.jsp"))
    assert (prog("orders/list.jsp"), "CALLS", prog("WEB-INF/tags/money.tag")) in rels
    assert (prog("orders/list.jsp"), "READS_FROM", key("Table", datastore="default", name="orders")) in rels


def test_names_never_values_in_pages():
    text = (
        '<a href="https://help.example.com/desk?topic=start&token=abc123">Help</a>\n'
        '<a href="edit.jsp?id=42&secret=hunter2">Edit</a>\n'
        '<% String password = "s3cr3t-value"; %>\n'
        '<input type="password" name="pw" value="prefilled-pass">\n'
    )
    out = run(text)
    blob = repr([o.payload for o in out.observations]) + repr(messages(out))
    for value in ("topic=start", "abc123", "hunter2", "s3cr3t-value", "prefilled-pass", "secret="):
        assert value not in blob, value
    names = {json.loads(k[1])["name"] for k in entities(out) if k[0] == "UIControl"}
    assert "link:https://help.example.com" in names and "link:edit.jsp" in names


def test_output_is_deterministic():
    text = (F25 / WEBAPP / "orders/edit.jsp").read_bytes()
    a, b = run(text, WEBAPP + "orders/edit.jsp"), run(text, WEBAPP + "orders/edit.jsp")
    assert [(o.kind, json.dumps(o.payload, sort_keys=True, default=str)) for o in a.observations] == \
           [(o.kind, json.dumps(o.payload, sort_keys=True, default=str)) for o in b.observations]


def test_binary_or_undecodable_input_does_not_raise():
    out = run(b"\x00\xff\xfe<%@ page %>\x00", WEBAPP + "bad.jsp")
    assert isinstance(out.observations, list)


# ---------------------------------------------------------------------- web.xml

WEBXML = WEBAPP + "WEB-INF/web.xml"


def test_servlet_mappings_become_endpoints_and_constraints_become_permissions():
    out = fixture(WEBXML, JspWebXml())
    ents, rels = entities(out), relations(out)
    assert ents[endpoint("/orders/save")]["servlet_class"] == "com.example.desk.OrderServlet"
    assert ents[endpoint("/reports/sales")]["servlet"] == "salesReport"
    assert (prog("reports/sales.jsp"), "EXPOSES", endpoint("/reports/sales")) in rels
    admin = key("Permission", realm="jsp", name="/admin/*")
    assert ents[admin]["transport"] == "CONFIDENTIAL"
    assert (role("admin"), "GRANTS", admin) in rels
    save = key("Permission", realm="jsp", name="POST /orders/save")
    assert ents[save]["methods"] == ["POST"]
    assert {(role("clerk"), "GRANTS", save), (role("admin"), "GRANTS", save)} <= rels
    assert (endpoint("/orders/save"), "REQUIRES_ROLE", role("clerk")) in rels
    assert (endpoint("/admin/purge.do"), "REQUIRES_ROLE", role("admin")) in rels
    assert (prog("reports/sales.jsp"), "REQUIRES_ROLE", role("manager")) in rels
    assert key("ConfigKey", name="datasource:jdbc/OrderDesk") in ents


def test_unmodelled_descriptor_parts_are_counted_and_context_param_values_never_kept():
    out = fixture(WEBXML, JspWebXml())
    counts = constructs(out)
    for cid in ("jsp:web.context-param", "jsp:web.filter", "jsp:web.login-config", "jsp:web.error-page"):
        assert counts.get((cid, False)), cid
    blob = repr([o.payload for o in out.observations])
    assert "fixture-not-a-real-password" not in blob


def test_url_pattern_matching():
    assert covers("/admin/*", "/admin/purge.do")
    assert covers("/admin/*", "/admin")
    assert covers("*.do", "/admin/purge.do")
    assert covers("/orders/save", "/orders/save")
    assert not covers("/orders/save", "/orders/save/x")
    assert not covers("/admin/*", "/administrator")


def test_entity_declarations_and_malformed_xml_are_refused():
    bomb = '<?xml version="1.0"?><!DOCTYPE web-app [<!ENTITY a "aaaa">]><web-app>&a;</web-app>'
    out = run(bomb, WEBXML, JspWebXml())
    assert not out.parsed and out.observations == []
    assert any("entity" in m for m in messages(out))
    out = run("<web-app><servlet>", WEBXML, JspWebXml())
    assert not out.parsed and any("well-formed" in m for m in messages(out))


def test_external_dtd_is_never_fetched():
    text = ('<?xml version="1.0"?>\n<!DOCTYPE web-app PUBLIC "-//Sun//DTD Web Application 2.3//EN" "http://java.sun.com/dtd/web-app_2_3.dtd">\n'
            "<web-app><servlet><servlet-name>a</servlet-name><servlet-class>x.A</servlet-class></servlet>"
            "<servlet-mapping><servlet-name>a</servlet-name><url-pattern>/a</url-pattern></servlet-mapping></web-app>")
    out = run(text, WEBXML, JspWebXml())
    assert out.parsed and endpoint("/a") in entities(out)
