"""The ASP.NET platform pack (repo_intel/platforms/aspnet): Web Forms, Razor Pages and views,
Blazor components (`aspnet.source`) and web.config (`aspnet.config`)."""

import hashlib
import json
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import validate_unit
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.platforms import packs, validate_pack
from repo_intel.platforms.aspnet import PACK, AspNetConfig, AspNetSource
from repo_intel.platforms.aspnet.razor import razor_regions, webforms_regions

FIXTURES = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures"
F26 = FIXTURES / "F26-aspnet-app" / "base"


def run(text: str | bytes, path: str, extractor=None):
    data = text if isinstance(text, bytes) else text.encode()
    return (extractor or AspNetSource()).extract(path, data, hashlib.sha256(data).hexdigest())


def fixture(path: str, extractor=None):
    return run((F26 / path).read_bytes(), path, extractor)


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
    return key("Program", platform="aspnet", name=name)


def screen(name: str) -> tuple:
    return key("Screen", platform="aspnet", name=name)


def endpoint(route: str, root: str = "@unit") -> tuple:
    """An Endpoint key; a Web Forms page's, which the linker keys, has its application root as `root`."""
    return key("Endpoint", component_root=root, protocol="http", method="ANY", route=route)


def role(name: str) -> tuple:
    return key("Role", realm="aspnet", name=name)


def control(container: str, name: str) -> tuple:
    return key("UIControl", container=container, name=name)


PAGES = sorted(str(p.relative_to(F26)) for p in F26.rglob("*") if p.suffix in AspNetSource.SUFFIXES)


# ---------------------------------------------------------------------- the pack


def test_pack_is_registered_and_valid():
    assert packs()["aspnet"] is PACK
    assert PACK.prefix == "aspx"
    assert validate_pack(PACK) == []
    assert {e.id for e in PACK.extractors} == {"aspnet.source", "aspnet.config"}
    assert len(PACK.constructs) >= 60


def test_extractors_claim_their_files():
    src, cfg = AspNetSource(), AspNetConfig()
    for path in ("a/B.aspx", "a/B.ascx", "Site.Master", "Pages/Index.cshtml", "Pages/Counter.razor"):
        assert src.matches(path), path
    assert not src.matches("a/B.aspx.cs") and not src.matches("a/b.html")
    for path in ("Web.config", "app/web.config", "Web.Release.config"):
        assert cfg.matches(path), path
    assert not cfg.matches("app.config") and not cfg.matches("packages.config")


def test_inventory_language_of_pages_is_aspnet():
    from repo_intel.extractors import detect_language

    for path in ("a/B.aspx", "a/B.ascx", "Site.Master", "Pages/Index.cshtml", "Pages/Counter.razor"):
        assert detect_language(path) == "aspnet", path
    assert detect_language("a/B.aspx.cs") == "csharp"


def test_the_pack_imports_extractors_only_inside_functions():
    import ast

    for p in (Path(__file__).resolve().parents[1] / "repo_intel" / "platforms" / "aspnet").glob("*.py"):
        tree = ast.parse(p.read_text())
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("repo_intel.extractors"), p.name
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("repo_intel.extractors") for a in node.names), p.name


def test_every_term_the_code_names_is_listed():
    import re

    terms = {t.name for t in PACK.terms}
    source = "".join(p.read_text() for p in (Path(__file__).resolve().parents[1] / "repo_intel" / "platforms" / "aspnet").glob("*.py"))
    used = set(re.findall(r'"(aspx:[A-Z][A-Za-z]*)"', source))
    assert used and used <= terms, sorted(used - terms)


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


# ---------------------------------------------------------------------- code regions


def _kinds(regions) -> list[tuple[str, str]]:
    return [(r.kind, r.code.strip()) for r in regions]


def test_razor_regions():
    text = (
        '@page "/x"\n@model Foo.Bar\n@* a comment @Model.Secret *@\n'
        "<p>@Model.Name and @(a + b) and me@example.com and @@</p>\n"
        "@if (User.IsInRole(\"Admin\")) {\n<b>admin</b>\n} else {\n<i>user</i>\n}\n"
        '@using (Html.BeginForm("Send", "Home")) {\n<input name="q">\n}\n'
        '<InputText @bind-Value="query.Postcode" @onclick="Go" />\n'
        "@code { int n; }\n"
    )
    regions = razor_regions(text)
    found = _kinds(regions)
    kinds = [k for k, _ in found]
    assert any(k == "directive" and c.startswith("page") for k, c in found)
    assert any(k == "directive" and c.startswith("model") for k, c in found)
    assert "comment" in kinds
    assert any("Model.Name" in c for _, c in found)
    assert any("a + b" in c for _, c in found)
    assert not any("example.com" in c for _, c in found)
    assert any("IsInRole" in c for _, c in found)
    # `@using (` opens a block, not a directive, so the form inside stays markup
    using = [c for k, c in found if "BeginForm" in c]
    assert using and not any(k == "directive" and "BeginForm" in c for k, c in found)
    assert not any("<input" in c for _, c in found)
    # Blazor directive attributes are markup
    assert not any("query.Postcode" in c or c == "onclick" for _, c in found)
    assert any("int n" in c for _, c in found)


def test_webforms_regions():
    text = '<%@ Page Language="C#" %>\n<%-- note --%>\n<% if (x) { %><p><%= Name %> <%: Safe %> <%# Eval("A") %> <%$ ConnectionStrings:Db %></p><% } %>\n'
    found = _kinds(webforms_regions(text))
    codes = [c for _, c in found]
    assert len(found) == 8
    assert any("Page Language" in c for c in codes)
    assert any("Eval" in c for c in codes) and any("ConnectionStrings:Db" in c for c in codes)


def test_unterminated_code_does_not_hang_or_raise():
    for text in ("@{ unterminated", "@(a + (b", "<% open", "@if (x) { <p>", '@page "', "@" * 5000):
        razor_regions(text)
        webforms_regions(text)
        run(text, "Pages/X.cshtml")


# ---------------------------------------------------------------------- Web Forms


def test_web_forms_page_master_code_behind_and_server_controls():
    out = fixture("desk/Orders/Edit.aspx")
    ents, rels = entities(out), relations(out)
    page = ents[prog("desk/Orders/Edit.aspx")]
    assert page["platform_kind"] == "aspx:Page" and page["inherits"] == "Desk.Orders.Edit"
    assert page["code_behind"] == "desk/Orders/Edit.aspx.cs" and page["master"] == "~/Site.Master"
    assert (prog("desk/Orders/Edit.aspx"), "CALLS", prog("desk/Site.Master")) in rels
    # The route depends on the application root (another file): the linker keys the Endpoint.
    assert not any(k[0] == "Endpoint" for k in ents)
    routes = [o.payload for o in out.observations if o.kind == "app_relative_route"]
    assert [(r["path"], r["root_marker"], r["subject"]["natural_key"]["name"]) for r in routes] == [("desk/Orders/Edit.aspx", "web.config", "desk/Orders/Edit.aspx")]
    assert routes[0]["attributes"] == {"framework": "webforms", "platform": "aspnet", "platform_kind": "aspx:PageRequest"}
    form = key("Form", screen="desk/Orders/Edit.aspx", name="orderForm")
    quantity = ents[control("desk/Orders/Edit.aspx#orderForm", "Quantity")]
    assert quantity["platform_kind"] == "aspx:ServerControl" and quantity["tag"] == "asp:TextBox"
    assert quantity["validation"] == [{"kind": "required"}, {"kind": "range", "max": "999", "min": "1", "type": "Integer"}]
    assert ents[control("desk/Orders/Edit.aspx#orderForm", "Save")]["handler"] == "Save_Click"
    # the form posts back to itself, and the Review button's PostBackUrl to the review page
    assert (form, "SUBMITS_TO", prog("desk/Orders/Edit.aspx")) in rels
    assert (form, "SUBMITS_TO", prog("desk/Orders/Review.aspx")) in rels


def test_sql_data_source_reads_a_table_and_names_the_connection_string_only():
    out = fixture("desk/Orders/Edit.aspx")
    rels = relations(out)
    assert (prog("desk/Orders/Edit.aspx"), "READS_FROM", key("Table", datastore="default", name="dbo.products")) in rels
    assert (prog("desk/Orders/Edit.aspx"), "REFERENCES_CONFIG", key("ConfigKey", name="connectionstring:DeskDb")) in rels


def test_redirects_role_checks_and_computed_transfer():
    out = fixture("desk/Orders/Edit.aspx")
    rels = relations(out)
    assert (screen("desk/Orders/Edit.aspx"), "NAVIGATES_TO", prog("desk/Login.aspx")) in rels
    assert {(prog("desk/Orders/Edit.aspx"), "REQUIRES_ROLE", role(r)) for r in ("Clerk", "Admin")} <= rels
    assert "Server.Transfer to a computed URL: target not resolved" in messages(out)
    blob = repr([o.payload for o in out.observations])
    assert "ReturnUrl=orders" not in blob and "QueryString" not in blob


def test_roles_is_user_in_role_keeps_its_callee():
    out = run('<%@ Page Language="C#" %>\n<% if (Roles.IsUserInRole("Auditor")) { } %>\n', "desk/A.aspx")
    assert (prog("desk/A.aspx"), "REQUIRES_ROLE", role("Auditor")) in relations(out)


def _summary(path: str, data: bytes | None = None, extractor=None) -> dict:
    """A file summary as the unit linker reads it: the file observation and what the extractor emits."""
    from repo_intel.extractors.base import obs, whole_file

    data = (F26 / path).read_bytes() if data is None else data
    blob = hashlib.sha256(data).hexdigest()
    rows = [obs("file", blob, whole_file(data), {"language": None, "size": len(data)}).row()]
    if extractor is not False:
        rows += [o.row() for o in (extractor or AspNetSource()).extract(path, data, blob).observations]
    return {"path": path, "observations": rows}


def _link(summaries: list[dict]):
    from repo_intel.compile.link import _Linker

    linker = _Linker({"name": ".", "paths": []}, summaries, [])
    linker.link_app_relative_routes()
    ents = {ref(o["payload"]): o["payload"]["attributes"] for o in linker.out if o["kind"] == "entity"}
    rels = {(ref(o["payload"]["subject"]), o["payload"]["predicate"], ref(o["payload"]["object"])) for o in linker.out if o["kind"] == "relation"}
    return ents, rels, linker.unknowns


def test_web_forms_routes_are_rooted_at_the_nearest_web_config():
    pages = ["desk/Default.aspx", "desk/Orders/Edit.aspx", "desk/Admin/Purge.aspx"]
    ents, rels, unknowns = _link([_summary(p) for p in pages] + [_summary("desk/Web.config", extractor=AspNetConfig())])
    assert unknowns == []
    for page, route in zip(pages, ["/Default.aspx", "/Orders/Edit.aspx", "/Admin/Purge.aspx"]):
        assert (prog(page), "EXPOSES", endpoint(route, "desk")) in rels
        assert ents[endpoint(route, "desk")]["app_root"] == "desk" and ents[endpoint(route, "desk")]["platform_kind"] == "aspx:PageRequest"
    # the route of a page is the name its <location path> rule gives the Permission
    config = entities(fixture("desk/Web.config", AspNetConfig()))
    assert key("Permission", realm="aspnet", name="/Orders/Edit.aspx") in config
    assert endpoint("/desk/Orders/Edit.aspx", "") not in ents and endpoint("/desk/Orders/Edit.aspx") not in ents


def test_web_forms_route_uses_the_deepest_web_config_and_the_repository_root():
    page = b'<%@ Page Language="C#" %><html><body><form id="f" runat="server"></form></body></html>'
    config = b"<configuration />"
    summaries = [_summary("apps/a/inner/Report.aspx", page), _summary("apps/a/Home.aspx", page), _summary("Top.aspx", page),
                 _summary("apps/a/Web.config", config, False), _summary("apps/a/inner/web.config", config, False), _summary("Web.config", config, False)]
    ents, rels, unknowns = _link(summaries)
    assert (prog("apps/a/inner/Report.aspx"), "EXPOSES", endpoint("/Report.aspx", "apps/a/inner")) in rels
    assert (prog("apps/a/Home.aspx"), "EXPOSES", endpoint("/Home.aspx", "apps/a")) in rels
    assert (prog("Top.aspx"), "EXPOSES", endpoint("/Top.aspx", "")) in rels
    assert ents[endpoint("/Top.aspx", "")]["app_root"] == "."
    assert unknowns == []


def test_web_forms_route_without_a_web_config_is_not_keyed():
    page = b'<%@ Page Language="C#" %><html><body></body></html>'
    ents, rels, unknowns = _link([_summary("site/Orders/Edit.aspx", page), _summary("other/Web.config", b"<configuration />", False)])
    assert not any(k[0] == "Endpoint" for k in ents) and not rels
    assert [(u["subject"]["natural_key"]["name"], u["predicate"]) for u in unknowns] == [("site/Orders/Edit.aspx", "EXPOSES")]
    assert "depends on the application root: not keyed" in unknowns[0]["reason"]


FOLDER_RULES = b"""<?xml version="1.0"?>
<configuration>
  <!-- only who may read this folder -->
  <system.web><authorization><allow roles="Admin" /><deny users="*" /></authorization></system.web>
  <location path="Purge.aspx"><system.web><authorization><deny users="?" /></authorization></system.web></location>
</configuration>
"""


@pytest.mark.parametrize("text, root", [
    (FOLDER_RULES, False),
    (b"<configuration><system.web><authorization><deny users='?' /></authorization></system.web></configuration>", False),
    (b"<configuration><location path='a.aspx'><system.web><authorization><deny users='?' /></authorization></system.web></location></configuration>", False),
    (b"<configuration />", True),
    (b"<configuration><system.web /></configuration>", True),
    (b"<configuration><system.web><authorization /><compilation debug='false' /></system.web></configuration>", True),
    (b"<configuration><appSettings><add key='Mode' value='x' /></appSettings><system.web><authorization><deny users='?' /></authorization></system.web></configuration>", True),
    (b"<configuration><system.webServer><security><authorization /></security></system.webServer></configuration>", True),
    (b"<configuration><location path='a'><system.webServer /></location></configuration>", True),
])
def test_a_web_config_roots_an_application_unless_it_only_sets_authorization(text, root):
    out = run(text, "site/Web.config", AspNetConfig())
    [marker] = [o.payload for o in out.observations if o.kind == "app_root"]
    assert marker == {"root": root}


def test_a_folder_web_config_with_only_authorization_does_not_move_the_root():
    pages = ["desk/Default.aspx", "desk/Admin/Purge.aspx"]
    summaries = [_summary(p) for p in pages] + [_summary("desk/Web.config", extractor=AspNetConfig()), _summary("desk/Admin/Web.config", FOLDER_RULES, AspNetConfig())]
    ents, rels, unknowns = _link(summaries)
    assert unknowns == []
    assert (prog("desk/Admin/Purge.aspx"), "EXPOSES", endpoint("/Admin/Purge.aspx", "desk")) in rels
    assert endpoint("/Purge.aspx", "desk/Admin") not in ents and ents[endpoint("/Admin/Purge.aspx", "desk")]["app_root"] == "desk"
    # The folder's rules are still read. The file does not know the application root, so
    # its Permissions are named by repository path, never "/" (the application's own rule).
    out = run(FOLDER_RULES, "desk/Admin/Web.config", AspNetConfig())
    folder = entities(out)
    assert {k for k in folder if k[0] == "Permission"} == {key("Permission", realm="aspnet", name="desk/Admin"), key("Permission", realm="aspnet", name="desk/Admin/Purge.aspx")}
    assert (role("Admin"), "GRANTS", key("Permission", realm="aspnet", name="desk/Admin")) in relations(out)


def test_two_web_forms_applications_in_one_unit_keep_their_routes_apart():
    page = b'<%@ Page Language="C#" %><html><body><form id="f" runat="server"></form></body></html>'
    config = b"<configuration><appSettings><add key='Mode' value='x' /></appSettings></configuration>"
    summaries = [_summary("shop/Default.aspx", page), _summary("admin/Default.aspx", page),
                 _summary("shop/Web.config", config, AspNetConfig()), _summary("admin/Web.config", config, AspNetConfig())]
    ents, rels, unknowns = _link(summaries)
    assert unknowns == []
    assert {k for k in ents if k[0] == "Endpoint"} == {endpoint("/Default.aspx", "shop"), endpoint("/Default.aspx", "admin")}
    assert (prog("shop/Default.aspx"), "EXPOSES", endpoint("/Default.aspx", "shop")) in rels
    assert (prog("admin/Default.aspx"), "EXPOSES", endpoint("/Default.aspx", "admin")) in rels


def test_master_page_and_user_control():
    rels = relations(fixture("desk/Site.Master"))
    assert (prog("desk/Site.Master"), "CALLS", prog("desk/Controls/Header.ascx")) in rels
    out = fixture("desk/Controls/Header.ascx")
    ents = entities(out)
    assert ents[prog("desk/Controls/Header.ascx")]["platform_kind"] == "aspx:UserControl"
    assert ents[screen("desk/Controls/Header.ascx")]["fragment"] is True
    assert not any(k[0] == "Endpoint" for k in ents)


# ---------------------------------------------------------------------- Razor


def test_razor_page_route_from_the_pages_folder_and_template():
    out = fixture("portal/Pages/Orders/Edit.cshtml")
    ents, rels = entities(out), relations(out)
    assert ents[screen("portal/Pages/Orders/Edit.cshtml")]["route"] == "/Orders/Edit/{id:int}"
    assert (prog("portal/Pages/Orders/Edit.cshtml"), "EXPOSES", endpoint("/Orders/Edit/{id:int}")) in rels
    assert ents[prog("portal/Pages/Orders/Edit.cshtml")]["injects"] == ["Portal.Services.IOrderService"]
    for r in ("Customer", "Support"):
        assert (endpoint("/Orders/Edit/{id:int}"), "REQUIRES_ROLE", role(r)) in rels
    assert (prog("portal/Pages/Orders/Edit.cshtml"), "CALLS", prog("portal/Pages/Shared/_OrderSummary.cshtml")) in rels


@pytest.mark.parametrize("path,page,route", [
    ("app/Pages/Index.cshtml", "@page", "/"),
    ("app/Pages/Orders/Index.cshtml", "@page", "/Orders"),
    ("app/Pages/Orders/Detail.cshtml", '@page "{id}"', "/Orders/Detail/{id}"),
    ("app/Pages/Orders/Detail.cshtml", '@page "/o/{id:int}"', "/o/{id:int}"),
    ("app/Areas/Admin/Pages/Users.cshtml", "@page", "/Users"),
])
def test_razor_page_routes(path, page, route):
    out = run(page + "\n<h1>x</h1>\n", path)
    assert endpoint(route) in entities(out)


def test_relative_route_outside_a_pages_folder_is_not_guessed():
    out = run('@page "{id}"\n<h1>x</h1>\n', "app/Views/Detail.cshtml")
    assert not any(k[0] == "Endpoint" for k in entities(out))
    assert any("route not computed" in m for m in messages(out))


def test_form_tag_helpers_bind_controls_and_submit_to_the_page():
    out = fixture("portal/Pages/Orders/Edit.cshtml")
    ents, rels = entities(out), relations(out)
    change = key("Form", screen="portal/Pages/Orders/Edit.cshtml", name="changeForm")
    assert ents[change]["page_handler"] == "Save"
    assert ents[control("portal/Pages/Orders/Edit.cshtml#changeForm", "Order.Quantity")]["binding"] == "Order.Quantity"
    assert (change, "SUBMITS_TO", prog("portal/Pages/Orders/Edit.cshtml")) in rels
    assert (control("portal/Pages/Orders/Edit.cshtml", "link:./Index"), "NAVIGATES_TO", prog("portal/Pages/Orders/Index.cshtml")) in rels


def test_view_start_layout_and_mvc_view():
    rels = relations(fixture("portal/Pages/_ViewStart.cshtml"))
    assert (prog("portal/Pages/_ViewStart.cshtml"), "CALLS", prog("portal/Pages/Shared/_Layout.cshtml")) in rels
    out = fixture("portal/Views/Home/Contact.cshtml")
    ents = entities(out)
    form = ents[key("Form", screen="portal/Views/Home/Contact.cshtml", name="form1")]
    assert form["kind"] == "html_helper" and form["controller"] == "Home" and form["action"] == "Send"
    assert ents[control("portal/Views/Home/Contact.cshtml#form1", "Email")]["tag"] == "Html.TextBoxFor"
    assert not any(k[0] == "Endpoint" for k in ents)
    assert any("MVC action" in m for m in messages(out))


def test_blazor_component_route_binding_and_authorize_view():
    out = fixture("portal/Components/Pages/Tracking.razor")
    ents, rels = entities(out), relations(out)
    me = prog("portal/Components/Pages/Tracking.razor")
    assert ents[me]["platform_kind"] == "aspx:RazorComponent" and ents[me]["injects"] == ["NavigationManager"]
    assert ents[endpoint("/tracking/{OrderNumber}")]["framework"] == "blazor"
    assert (me, "EXPOSES", endpoint("/tracking/{OrderNumber}")) in rels
    assert (endpoint("/tracking/{OrderNumber}"), "REQUIRES_ROLE", role("Customer")) in rels
    assert (screen("portal/Components/Pages/Tracking.razor"), "REQUIRES_ROLE", role("Support")) in rels
    postcode = ents[control("portal/Components/Pages/Tracking.razor#form1", "query.Postcode")]
    assert postcode["binding"] == "query.Postcode" and postcode["tag"] == "InputText"


# ---------------------------------------------------------------------- values never kept


def test_names_never_values_in_pages():
    text = (
        '<%@ Page Language="C#" %>\n<form runat="server" id="f">\n'
        '<asp:TextBox ID="Pw" runat="server" TextMode="Password" Text="prefilled-pass" />\n'
        '<asp:SqlDataSource ID="S" runat="server" ConnectionString="Server=x;Password=hunter2" SelectCommand="SELECT a FROM t WHERE k = \'abc123\'" />\n'
        '<asp:HyperLink ID="H" runat="server" NavigateUrl="https://help.example.com/desk?token=zz9" />\n'
        "</form>\n<% Response.Redirect(\"Next.aspx?secret=qq7\"); %>\n"
    )
    out = run(text, "desk/V.aspx")
    blob = repr([o.payload for o in out.observations]) + repr(messages(out))
    for value in ("prefilled-pass", "hunter2", "Password=", "zz9", "qq7"):
        assert value not in blob, value
    # SQL text in a query node is kept as written (contract rule 7)
    assert any(n.get("op") == "query" for o in out.observations if o.kind == "behavior" for n in _walk(o.payload))
    # a redirect keeps the query's parameter names, never their values
    redirects = [n["target"]["value"] for o in out.observations if o.kind == "behavior" for n in _walk(o.payload) if n.get("op") == "redirect"]
    assert redirects == ["Next.aspx?secret="]


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_output_is_deterministic():
    data = (F26 / "desk/Orders/Edit.aspx").read_bytes()
    a, b = run(data, "desk/Orders/Edit.aspx"), run(data, "desk/Orders/Edit.aspx")
    assert [json.dumps(o.payload, sort_keys=True, default=str) for o in a.observations] == \
           [json.dumps(o.payload, sort_keys=True, default=str) for o in b.observations]


# ---------------------------------------------------------------------- web.config


def test_web_config_connection_strings_app_settings_and_authorization():
    out = fixture("desk/Web.config", AspNetConfig())
    ents, rels = entities(out), relations(out)
    db = ents[key("ConfigKey", name="connectionstring:DeskDb")]
    assert db["platform_kind"] == "aspx:ConnectionString" and db["provider"] == "System.Data.SqlClient"
    assert key("ConfigKey", name="appsetting:SmtpHost") in ents and key("ConfigKey", name="appsetting:ReviewQueue") in ents
    root = ents[key("Permission", realm="aspnet", name="/")]
    assert root["anonymous_denied"] is True
    edit = key("Permission", realm="aspnet", name="/Orders/Edit.aspx")
    assert ents[edit]["all_denied"] is True
    assert {(role("Clerk"), "GRANTS", edit), (role("Admin"), "GRANTS", edit)} <= rels
    assert (prog("desk/Orders/Edit.aspx"), "REQUIRES_ROLE", role("Clerk")) in rels
    assert ents[key("Permission", realm="aspnet", name="/Login.aspx")]["all_allowed"] is True
    assert role("*") not in ents and role("?") not in ents
    counts = constructs(out)
    for cid in ("aspx:config.authentication", "aspx:config.custom-errors", "aspx:config.session-state"):
        assert counts.get((cid, False)), cid


def test_web_config_values_are_never_kept():
    out = fixture("desk/Web.config", AspNetConfig())
    blob = repr([o.payload for o in out.observations])
    for value in ("fixture-not-a-real-password", "db.internal", "mail.internal", "orders-review", "User Id"):
        assert value not in blob, value


def test_web_config_entity_declarations_and_malformed_xml_are_refused():
    bomb = '<?xml version="1.0"?><!DOCTYPE c [<!ENTITY a "aaaa">]><configuration>&a;</configuration>'
    out = run(bomb, "Web.config", AspNetConfig())
    assert not out.parsed and out.observations == []
    out = run("<configuration><appSettings>", "Web.config", AspNetConfig())
    assert not out.parsed and any("well-formed" in m for m in messages(out))
