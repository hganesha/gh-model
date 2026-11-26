"""The web platform pack: routes and pages as screens, forms and controls, design tokens and
route guards, from HTML, JSX/TSX, Vue and Angular sources and from stylesheets."""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from repo_intel import extractors, platforms  # noqa: F401
from repo_intel.compile.fixtures import FIXTURES_DIR
from repo_intel.compile.ontology import EntityKind, check_relation
from repo_intel.platforms.web import PACK, styles

BLOB = "ab" * 32


def run(path, text):
    """Every web extractor that claims `path`, merged: (entities, relations, constructs, diagnostics)."""
    data = text.encode() if isinstance(text, str) else text
    found = [e for e in PACK.extractors if e.matches(path)]
    assert found, f"no web extractor claims {path}"
    out = {"entities": [], "relations": [], "constructs": {}, "diagnostics": []}
    for e in found:
        r = e.extract(path, data, BLOB)
        for o in r.observations:
            if o.kind == "entity":
                out["entities"].append(o.payload)
            elif o.kind == "relation":
                out["relations"].append({**o.payload, "evidence_class": o.evidence_class})
            elif o.kind == "construct":
                seen, modelled = out["constructs"].get(o.payload["construct"], (0, 0))
                out["constructs"][o.payload["construct"]] = (seen + 1, modelled + o.payload["modelled"])
        out["diagnostics"] += [d.message for d in r.diagnostics]
    return out


def names(out, kind, key="name"):
    return sorted(e["natural_key"][key] for e in out["entities"] if e["kind"] == kind)


def entity(out, kind, **key):
    found = [e for e in out["entities"] if e["kind"] == kind and all(e["natural_key"].get(k) == v for k, v in key.items())]
    assert len(found) == 1, (kind, key, [e["natural_key"] for e in out["entities"] if e["kind"] == kind])
    return found[0]


def edges(out, predicate):
    """(subject name, object name) pairs, '?' marking an object the file does not declare."""
    def label(ref):
        if isinstance(ref, str):
            return ref
        key = ref["natural_key"]
        return str(key.get("name", key.get("route"))) + ("" if ref.get("declare", True) else "?")
    return sorted({(label(r["subject"]), label(r["object"])) for r in out["relations"] if r["predicate"] == predicate})


# ------------------------------------------------------------------ the pack


def test_the_pack_is_registered_and_valid():
    assert platforms.packs()["web"] is PACK
    assert platforms.validate_pack(PACK) == []
    kinds = {t.name: t.core for t in PACK.terms}
    assert kinds["web:Route"] is EntityKind.SCREEN and kinds["web:Page"] is EntityKind.SCREEN
    assert kinds["web:Form"] is EntityKind.FORM and kinds["web:Control"] is EntityKind.UI_CONTROL
    assert {kinds[t] for t in ("web:CssVariable", "web:ScssVariable", "web:ThemeToken")} == {EntityKind.STYLE_TOKEN}
    assert kinds["web:Role"] is EntityKind.ROLE and PACK.term("web:Role").fidelity == "exact"
    assert "web:Guard" not in kinds  # a guard lists roles; it is not one
    assert len(PACK.constructs) >= 40
    assert {e.id for e in PACK.extractors} == {"web.markup", "web.script", "web.styles"}
    assert not PACK.supersedes


WEB_PACK_DIR = Path(platforms.__file__).parent / "web"
REPO_ROOT = Path(platforms.__file__).parents[2]


def test_importing_the_pack_first_still_registers_it():
    # Discovery refuses a pack that is still importing; a pack that imports
    # repo_intel.extractors at module level is still importing when discovery reaches it.
    code = "import repo_intel.platforms.web\nfrom repo_intel import platforms\nassert 'web' in platforms.packs(), sorted(platforms.packs())\n"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]


@pytest.mark.parametrize("module", sorted(p.name for p in WEB_PACK_DIR.glob("*.py")))
def test_no_module_level_import_of_the_extractors_package(module):
    def run_at_import(node):  # every statement except those inside function bodies
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                yield child
                yield from run_at_import(child)

    for n in run_at_import(ast.parse((WEB_PACK_DIR / module).read_text())):
        if isinstance(n, ast.Import):
            assert not any(a.name.startswith("repo_intel.extractors") for a in n.names), (module, n.lineno)
        elif isinstance(n, ast.ImportFrom):
            assert not (n.module or "").startswith("repo_intel.extractors"), (module, n.lineno)
            assert not (n.module == "repo_intel" and any(a.name == "extractors" for a in n.names)), (module, n.lineno)


@pytest.mark.parametrize("path, claimed", [
    ("src/App.tsx", True), ("index.html", True), ("src/views/A.vue", True), ("src/styles/a.scss", True),
    ("node_modules/react-router/index.js", False), ("dist/app.js", False), ("src/App.test.tsx", False),
    ("src/Button.stories.tsx", False), ("src/types.d.ts", False), ("public/vendor.min.js", False),
])
def test_which_files_the_pack_reads(path, claimed):
    assert any(e.matches(path) for e in PACK.extractors) is claimed


# ------------------------------------------------------------------ screens


REACT_ROUTES = """
import { lazy } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import Shell from "./Shell";
import Home from "./pages/Home";
import Orders from "./pages/Orders";
const Admin = lazy(() => import("./pages/Admin"));
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Shell />}>
          <Route index element={<Home />} />
          <Route path="orders" element={<Orders />} />
          <Route path="admin" element={<RequireRole roles={["admin", "auditor"]}><Admin /></RequireRole>} />
          <Route path="legacy" element={<Navigate to="/orders" />} />
        </Route>
        <Route path={dynamicPath} element={<Orders />} />
      </Routes>
    </BrowserRouter>
  );
}
"""


def test_react_router_elements_are_screens():
    out = run("src/App.tsx", REACT_ROUTES)
    assert names(out, "Screen") == ["/", "/admin", "/orders"]
    admin = entity(out, "Screen", name="/admin")["attributes"]
    assert admin["platform_kind"] == "web:Route" and admin["component"] == "Admin" and admin["lazy"] is True
    assert admin["guards"] == ["RequireRole"] and admin["layouts"] == ["Shell"]
    assert edges(out, "REQUIRES_ROLE") == [("/admin", "admin"), ("/admin", "auditor")]
    role = entity(out, "Role", name="admin")
    assert role["natural_key"] == {"realm": "web", "name": "admin"}
    assert role["attributes"]["platform_kind"] == "web:Role" and "fidelity" not in role["attributes"]  # exact
    assert ("@symbol:Orders", "/orders") in edges(out, "RENDERS")  # the linker resolves it, or falls back to the package
    assert ("/orders", "src/pages/Orders.tsx?") in edges(out, "CONTAINS")  # the view, when the unit declares one
    assert out["constructs"]["web:redirect-route"] == (1, 0)
    assert out["constructs"]["web:lazy-route"] == (1, 1)
    assert any("not literal" in d or "dynamic" in d for d in out["diagnostics"])


def test_router_objects_are_screens():
    out = run("src/router.tsx", """
import { createBrowserRouter } from "react-router-dom";
import Root from "./Root";
export const router = createBrowserRouter([
  { path: "/", element: <Root />, children: [
    { index: true, element: <Home /> },
    { path: "reports/:id", lazy: () => import("./Report"), handle: { roles: ["finance"] } },
  ] },
]);
""")
    assert names(out, "Screen") == ["/", "/reports/:id"]
    assert edges(out, "REQUIRES_ROLE") == [("/reports/:id", "finance")]
    assert out["constructs"]["web:route-object"][1] == 2


def test_vue_router_routes_are_screens():
    out = run("src/router/index.ts", """
import { createRouter, createWebHistory } from "vue-router";
import Cart from "../views/Cart.vue";
const routes = [
  { path: "/", redirect: "/cart" },
  { path: "/cart", name: "cart", component: Cart },
  { path: "/account", component: () => import("../views/Account.vue"), meta: { requiresAuth: true, roles: ["customer"] } },
];
export default createRouter({ history: createWebHistory(), routes });
""")
    assert names(out, "Screen") == ["/account", "/cart"]
    account = entity(out, "Screen", name="/account")["attributes"]
    assert account["framework"] == "vue-router" and account["requires_auth"] is True
    assert edges(out, "REQUIRES_ROLE") == [("/account", "customer")]
    assert ("/cart", "src/views/Cart.vue?") in edges(out, "CONTAINS")


def test_angular_routes_are_screens_and_children_inherit_guards():
    out = run("src/app/app-routing.module.ts", """
import { NgModule } from "@angular/core";
import { RouterModule, Routes } from "@angular/router";
const routes: Routes = [
  { path: "admin", component: ShellComponent, canActivate: [AuthGuard], data: { roles: ["admin", "support"] }, children: [
    { path: "", component: DashboardComponent },
    { path: "users", component: UsersComponent, data: { roles: ["admin"] } },
    { path: "reports", loadChildren: () => import("./reports/reports.module").then(m => m.ReportsModule) },
  ] },
  { path: "**", redirectTo: "admin" },
];
@NgModule({ imports: [RouterModule.forRoot(routes)], exports: [RouterModule] })
export class AppRoutingModule {}
""")
    assert names(out, "Screen") == ["/admin", "/admin/reports", "/admin/users"]
    assert entity(out, "Screen", name="/admin/reports")["attributes"]["guards"] == ["AuthGuard"]
    roles = edges(out, "REQUIRES_ROLE")
    assert ("/admin/users", "admin") in roles and ("/admin/users", "support") not in roles  # its own list narrows the parent's
    assert ("/admin/reports", "support") in roles


def test_feature_module_routes_are_not_guessed():
    out = run("src/app/orders/orders-routing.module.ts", """
const routes: Routes = [{ path: "list", component: OrderListComponent }];
@NgModule({ imports: [RouterModule.forChild(routes)] })
export class OrdersRoutingModule {}
""")
    assert names(out, "Screen") == []
    assert out["diagnostics"]


@pytest.mark.parametrize("path, route, construct", [
    ("app/orders/[id]/page.tsx", "/orders/[id]", "web:nextjs-app-page"),
    ("src/app/(shop)/cart/page.tsx", "/cart", "web:nextjs-app-page"),
    ("app/page.tsx", "/", "web:nextjs-app-page"),
])
def test_next_app_router_files_are_screens(path, route, construct):
    out = run(path, "export default function Page() { return <main><a href=\"/help\">Help</a></main>; }")
    assert names(out, "Screen") == [route]
    assert out["constructs"][construct] == (1, 1)


def test_pages_directory_needs_a_next_signal():
    page = "import Link from 'next/link';\nexport default function Orders() { return <Link href=\"/\">Home</Link>; }"
    assert names(run("pages/orders/index.tsx", page), "Screen") == ["/orders"]
    plain = "export default function Orders() { return <div>orders</div>; }"
    assert names(run("src/pages/orders.tsx", plain), "Screen") == []  # a folder called pages is not a router
    assert names(run("pages/api/orders.ts", "import type { NextApiRequest } from 'next';\nexport default function h() {}"), "Screen") == []


def test_static_html_documents_are_pages():
    out = run("public/about.html", """<!doctype html>
<html><head><title>About us</title><link rel="stylesheet" href="../src/styles/tokens.css"></head>
<body>
  <a href="contact.html">Contact</a> <a href="/orders">Orders</a> <a href="https://status.example.com/x">Status</a> <a href="#top">Top</a>
  <form action="/search" method="get"><input name="q" required><button type="submit">Go</button></form>
  <p style="padding: var(--space-4)">hi</p>
</body></html>""")
    page = entity(out, "Screen", name="public/about.html")["attributes"]
    assert page["platform_kind"] == "web:Page" and page["title"] == "About us"
    assert page["stylesheets"] == ["src/styles/tokens.css"]
    nav = edges(out, "NAVIGATES_TO")
    assert ("link:contact.html", "public/contact.html?") in nav and ("link:/orders", "/orders?") in nav
    assert not [n for n in nav if "status" in n[0]]  # an external link has no screen to go to
    assert entity(out, "UIControl", name="link:https://status.example.com")["attributes"]["external"] is True
    assert ("form1", "/search?") in edges(out, "SUBMITS_TO")
    assert ("public/about.html", "--space-4?") in edges(out, "STYLED_BY")


def test_an_html_fragment_with_a_form_is_a_view():
    out = run("src/app/users/users.component.html", """<form #f="ngForm" (ngSubmit)="search()">
  <input name="query" [(ngModel)]="query" />
  <button type="submit">Search</button>
</form>
<a routerLink="/users/new">New</a>""")
    view = entity(out, "Screen", name="src/app/users/users.component.html")["attributes"]
    assert view["platform_kind"] == "web:View" and view["framework"] == "angular"
    assert entity(out, "UIControl", name="query")["attributes"]["binds"] == "query"
    assert ("link:/users/new", "/users/new?") in edges(out, "NAVIGATES_TO")


def test_a_guard_component_in_a_view_requires_its_roles_and_is_kept_as_an_attribute():
    out = run("src/views/Reports.tsx", """
import RequireRole from "../components/RequireRole";
export default function Reports() {
  return <RequireRole roles={["finance", "audit"]}><form><input name="from" /></form></RequireRole>;
}
""")
    view = entity(out, "Screen", name="src/views/Reports.tsx")["attributes"]
    assert view["platform_kind"] == "web:View" and view["guards"] == ["RequireRole"]
    assert edges(out, "REQUIRES_ROLE") == [("src/views/Reports.tsx", "audit"), ("src/views/Reports.tsx", "finance")]
    for name in ("audit", "finance"):  # the role is the entity; the guard that lists it is not
        role = entity(out, "Role", name=name)
        assert role["attributes"]["platform_kind"] == "web:Role" and not {"guard", "guards", "fidelity"} & set(role["attributes"])


def test_a_component_without_forms_or_links_is_not_a_screen():
    assert names(run("src/components/Badge.tsx", "export function Badge() { return <span className=\"badge\">new</span>; }"), "Screen") == []


# ------------------------------------------------------------------ forms and controls


def test_control_names_follow_the_contract_order():
    # name, id, binding path, link:<target>, submit or button:<text>, else <type><n>: type is
    # an input's type attribute (text when absent), else the tag; n counts only the
    # container's controls of that type that reach this last rule.
    out = run("src/partials/signup.html", """<form id="signup" [formGroup]="form">
  <input type="text">
  <input name="email" id="ignored-when-named">
  <input id="phone">
  <input formControlName="city">
  <input type="text">
  <input type="button" value="Preview">
  <button type="button"></button>
  <button type="button">Clear</button>
  <a href="#terms">Terms</a>
  <a href="?page=2">Next</a>
  <a href="/help?topic=signup#top">Help</a>
  <a href="https://example.com/a/b?token=x">Elsewhere</a>
  <input type="submit" value="Send">
</form>""")
    container = "src/partials/signup.html#signup"
    controls = sorted(e["natural_key"]["name"] for e in out["entities"] if e["kind"] == "UIControl" and e["natural_key"]["container"] == container)
    assert controls == sorted([
        "text1", "email", "phone", "city", "text2",
        "button:Preview", "button1", "button:Clear",
        "link1", "link2", "link:/help", "link:https://example.com",  # a fragment or a query alone leaves no target
        "submit",
    ])


def _controls(out, container):
    return [e["natural_key"]["name"] for e in out["entities"] if e["kind"] == "UIControl" and e["natural_key"]["container"] == container]


def test_fallback_names_count_only_the_controls_that_fall_back():
    out = run("src/partials/filters.html", """<form id="f">
  <input>
  <input type="date">
  <input name="q">
  <select></select>
  <input type="Date">
  <textarea></textarea>
  <input>
  <select></select>
</form>
<form id="g"><input><select></select></form>""")
    assert _controls(out, "src/partials/filters.html#f") == ["text1", "date1", "q", "select1", "date2", "textarea1", "text2", "select2"]
    assert _controls(out, "src/partials/filters.html#g") == ["text1", "select1"]  # counted per container
    # Naming one control, or adding a named one, renames no other control.
    named = run("src/partials/filters.html", """<form id="f">
  <input name="first">
  <input type="date">
  <input name="q">
  <input name="added">
  <select></select>
  <input type="Date">
  <textarea></textarea>
  <input>
  <select></select>
</form>""")
    assert _controls(named, "src/partials/filters.html#f") == ["first", "date1", "q", "added", "select1", "date2", "textarea1", "text1", "select2"]


def test_repeated_names_in_one_container_get_a_suffix():
    out = run("src/partials/contact.html", """<form id="c">
  <button type="submit">Send</button>
  <button type="submit">Send and copy me</button>
  <input type="radio" name="size" value="s"><input type="radio" name="size" value="m"><input type="radio" name="size" value="l">
  <a href="mailto:sales@example.com">Sales</a>
  <a href="mailto:help@example.com">Help</a>
  <a href="/orders">Orders</a>
  <a href="/orders?page=2">More orders</a>
</form>
<a href="/orders">Orders</a>""")
    assert _controls(out, "src/partials/contact.html#c") == [
        "submit", "submit~2", "size", "size~2", "size~3", "link:mailto:", "link:mailto:~2", "link:/orders", "link:/orders~2"]
    assert _controls(out, "src/partials/contact.html") == ["link:/orders"]  # another container: no suffix
    assert "example.com" not in repr(out["entities"])  # no mail address is copied


def test_blank_name_and_binding_values_do_not_name_a_control():
    out = run("src/partials/blank.html", """<form id="b">
  <input type="text" name="  ">
  <input formControlName=" " id=" ">
  <input formControlName=" city ">
  <input name=" email ">
</form>""")
    controls = {e["natural_key"]["name"]: e["attributes"] for e in out["entities"] if e["kind"] == "UIControl"}
    assert sorted(controls) == ["city", "email", "text1", "text2"]
    assert controls["city"]["binds"] == "city" and controls["email"]["binds"] == "email"
    assert "binds" not in controls["text1"] and "binds" not in controls["text2"]


def test_button_text_is_all_descendant_text_but_never_computed():
    html = run("src/partials/editor.html", """<form id="e">
  <button type="button"><b>Preview</b> page</button>
  <button type="button"><span><i class="icon"></i>Save</span>  <em>draft</em></button>
  <button type="button">Delete {{ count }} rows</button>
  <button type="button">A label that is far too long to be a control name at all</button>
</form>""")
    assert _controls(html, "src/partials/editor.html#e") == ["button:Preview page", "button:Save draft", "button1", "button2"]
    jsx = run("src/views/Editor.tsx", """
export default function Editor({ count, ok }) {
  return (
    <form name="e">
      <button type="button"><b>Preview</b> page</button>
      <button type="button">Delete {count} rows</button>
      <button type="button">{ok && <b>Done</b>}</button>
      <Button><Icon /> Export <em>all</em></Button>
    </form>
  );
}
""")
    # Expression text is left out: what is left is the static text around it.
    assert _controls(jsx, "src/views/Editor.tsx#e") == ["button:Preview page", "button:Delete rows", "button1", "button:Export all"]
    assert entity(jsx, "UIControl", name="button:Preview page")["attributes"]["label"] == "Preview page"


def test_urls_keep_query_parameter_names_but_never_values():
    out = run("src/partials/search.html", """<form id="s" action="/search?q=shoes&page=2#results" method="get">
  <a href="/help?topic=signup&token=abc123#top">Help</a>
  <a href="https://user:pw@example.com/a/b?token=x">Elsewhere</a>
  <a href="login.cfm?token=abc123">Log in</a>
  <a [href]="'/orders?key=' + secretKey">Orders</a>
</form>
<form id="t" action="https://pay.example.com/charge?amount=10"></form>""")
    form = entity(out, "Form", name="s")["attributes"]
    assert form["action"] == "/search?q=&page="
    controls = {e["natural_key"]["name"]: e["attributes"] for e in out["entities"] if e["kind"] == "UIControl"}
    assert controls["link:/help"]["target"] == "/help?topic=&token="
    assert controls["link:https://example.com"]["target"] == "https://example.com"
    assert controls["link:login.cfm"]["target"] == "login.cfm?token="
    assert entity(out, "Form", name="t")["attributes"]["action"] == "https://pay.example.com"
    text = repr(out["entities"]) + repr(out["relations"]) + repr(out["diagnostics"])
    for value in ("shoes", "abc123", "user:pw", "/a/b", "amount=10", "results", "#top"):
        assert value not in text, value


def _spans(path, data, kind="UIControl"):
    ext = next(e for e in PACK.extractors if e.matches(path))
    result = ext.extract(path, data, BLOB)
    return {o.payload["natural_key"]["name"]: o.evidence.span for o in result.observations if o.kind == "entity" and o.payload["kind"] == kind}


def test_non_ascii_markup_is_read_as_utf8_with_byte_spans():
    source = """<!doctype html><html><head><title>Bestellübersicht</title></head><body>
<p>Größe wählen – à la carte</p>
<form id="bestellung"><button type="button">Bestätigen &amp; schließen</button>
<input type="button" value="Zurück"><input name="straße"></form></body></html>"""
    data = source.encode("utf-8")
    out = run("public/bestellung.html", data)
    assert entity(out, "Screen", name="public/bestellung.html")["attributes"]["title"] == "Bestellübersicht"
    assert _controls(out, "public/bestellung.html#bestellung") == ["button:Bestätigen &amp; schließen", "button:Zurück", "straße"]
    # Spans are byte offsets into the file, past every multi-byte character before them.
    spans = _spans("public/bestellung.html", data)
    for name, opening in (("straße", '<input name="straße">'), ("button:Zurück", '<input type="button" value="Zurück">')):
        assert data[spans[name].start_byte:spans[name].end_byte] == opening.encode("utf-8"), name
    # A file that is not valid UTF-8 is read as Latin-1, one character per byte.
    latin = '<form id="l"><button type="button">Grüße</button><input name="x"></form>'.encode("latin-1")
    out = run("src/partials/latin.html", latin)
    assert _controls(out, "src/partials/latin.html#l") == ["button:Grüße", "x"]
    assert _spans("src/partials/latin.html", latin)["x"].start_byte == latin.index(b'<input name="x">')


def test_non_ascii_vue_templates_and_styles_keep_byte_spans():
    data = """<template>
  <form @submit.prevent="save"><button type="button">Löschen</button><input name="straße" /></form>
</template>
<script setup>
// Schließen
async function save() { await fetch("/api/adresse", { method: "POST" }); }
</script>
<style>
/* Grün */
.x { --farbe: #00ff00; }
</style>
""".encode("utf-8")
    spans = _spans("src/views/Adresse.vue", data)
    assert data[spans["straße"].start_byte:spans["straße"].end_byte] == '<input name="straße" />'.encode()
    assert data[spans["button:Löschen"].start_byte:spans["button:Löschen"].end_byte] == b'<button type="button">'
    assert entity(run("src/views/Adresse.vue", data), "Form", name="form1")["attributes"]["submits"] == ["POST /api/adresse"]
    token = _spans("src/views/Adresse.vue", data, "StyleToken")["--farbe"]
    assert data[token.start_byte:token.end_byte].startswith(b"--farbe")


def test_non_ascii_angular_inline_templates_keep_byte_spans():
    data = """import { Component } from "@angular/core";
// Übersicht
@Component({ selector: "x", template: `<p>Grüße</p><form><button type="button">Prüfen</button><input name="größe"></form>` })
export class X {}
""".encode("utf-8")
    spans = _spans("src/app/x.component.ts", data)
    assert sorted(spans) == ["button:Prüfen", "größe"]
    assert data[spans["größe"].start_byte:spans["größe"].end_byte] == '<input name="größe">'.encode()


NEW_ORDER = """
import { useForm } from "react-hook-form";
import { useNavigate } from "react-router-dom";
import { TextField, Button } from "@mui/material";

async function createOrder(values) {
  return fetch("/api/orders", { method: "POST", body: JSON.stringify(values) });
}

export default function NewOrder() {
  const { register, handleSubmit } = useForm();
  const navigate = useNavigate();
  const onSubmit = async (values) => { await createOrder(values); navigate("/orders"); };
  return (
    <form name="new-order" onSubmit={handleSubmit(onSubmit)}>
      <TextField label="Customer" {...register("customer", { required: true })} />
      <input type="number" {...register("quantity", { min: 1 })} />
      <select name="status"><option>open</option></select>
      <textarea name="notes" maxLength={500} />
      <Button type="submit">Place order</Button>
      <a href={`/orders/${id}`}>Back</a>
    </form>
  );
}
"""


def test_jsx_forms_controls_and_submit_targets():
    out = run("src/pages/NewOrder.tsx", NEW_ORDER)
    form = entity(out, "Form", name="new-order")
    assert form["natural_key"] == {"screen": "src/pages/NewOrder.tsx", "name": "new-order"}
    assert form["attributes"]["form_library"] == "react-hook-form" and form["attributes"]["submits"] == ["POST /api/orders"]
    controls = {e["natural_key"]["name"]: e["attributes"] for e in out["entities"] if e["kind"] == "UIControl"}
    assert controls["customer"]["control_type"] == "input" and controls["customer"]["binds"] == "customer"
    assert controls["customer"]["validation"] == ["required"]
    assert controls["quantity"]["input_type"] == "number"
    assert controls["status"]["control_type"] == "select" and controls["notes"]["control_type"] == "textarea"
    assert controls["submit"]["control_type"] == "button"
    dynamic = [a for a in controls.values() if a.get("target") == "dynamic"]
    assert len(dynamic) == 1
    submits = [r for r in out["relations"] if r["predicate"] == "SUBMITS_TO"]
    assert len(submits) == 1 and submits[0]["evidence_class"] == "E3"
    assert submits[0]["object"]["kind"] == "Endpoint" and submits[0]["object"]["declare"] is False
    assert submits[0]["object"]["natural_key"] == {"component_root": "@unit", "protocol": "http", "method": "POST", "route": "/api/orders"}
    assert ("src/pages/NewOrder.tsx", "/orders?") in edges(out, "NAVIGATES_TO")
    assert any("/api/orders" in d for d in out["diagnostics"])
    assert any("not a literal" in d for d in out["diagnostics"])


def test_a_submit_handler_that_cannot_be_followed_links_nothing():
    out = run("src/pages/Pay.tsx", """
import { pay } from "../api";
export default function Pay() {
  return <form name="pay" onSubmit={pay}><input name="amount" /><button>Pay</button></form>;
}
""")
    assert edges(out, "SUBMITS_TO") == []
    assert entity(out, "Form", name="pay")["attributes"]["submit_handler"] == "pay"


def test_a_computed_url_is_not_guessed():
    out = run("src/pages/Pay.tsx", """
export default function Pay() {
  async function save(e) { e.preventDefault(); await fetch(`${API}/payments`, { method: "POST" }); }
  return <form name="pay" onSubmit={save}><button>Pay</button></form>;
}
""")
    assert edges(out, "SUBMITS_TO") == []


def test_vue_single_file_components():
    out = run("src/views/Checkout.vue", """<template>
  <form @submit.prevent="placeOrder">
    <input v-model="form.email" type="email" required />
    <el-input v-model="form.coupon" />
    <router-link to="/cart">Back</router-link>
    <button type="submit">Pay</button>
  </form>
</template>

<script setup lang="ts">
import { reactive } from "vue";
import { useRouter } from "vue-router";
const router = useRouter();
const form = reactive({ email: "", coupon: "" });
async function placeOrder() {
  await fetch("/api/checkout", { method: "POST", body: JSON.stringify(form) });
  router.push("/thanks");
}
</script>

<style scoped lang="scss">
@use "../styles/variables" as *;
.checkout { --accent: #ff8800; padding: $gutter; color: var(--color-primary); border-color: var(--accent); }
</style>
""")
    view = entity(out, "Screen", name="src/views/Checkout.vue")["attributes"]
    assert view["framework"] == "vue" and view["stylesheets"] == ["src/styles/variables"]
    assert entity(out, "UIControl", name="form.email")["attributes"]["input_type"] == "email"
    assert entity(out, "UIControl", name="form.coupon")["attributes"]["control_type"] == "input"
    assert entity(out, "Form", name="form1")["attributes"]["submits"] == ["POST /api/checkout"]
    assert ("link:/cart", "/cart?") in edges(out, "NAVIGATES_TO")
    assert ("src/views/Checkout.vue", "/thanks?") in edges(out, "NAVIGATES_TO")
    styled = edges(out, "STYLED_BY")
    accent = [r for r in out["relations"] if r["predicate"] == "STYLED_BY" and r["object"]["natural_key"]["name"] == "--accent"]
    assert [r["evidence_class"] for r in accent] == ["E2"]  # declared in this file: no other file needed
    assert ("src/views/Checkout.vue", "$gutter?") in styled
    assert entity(out, "StyleToken", name="--accent")["natural_key"]["system"] == "src/views/Checkout.vue"


def test_angular_inline_templates_and_reactive_forms():
    out = run("src/app/users/user-form.component.ts", """
import { Component } from "@angular/core";
import { HttpClient } from "@angular/common/http";
@Component({
  selector: "app-user-form",
  template: `
    <form [formGroup]="form" (ngSubmit)="save()">
      <input formControlName="email" type="email" />
      <select formControlName="role"><option value="admin">Admin</option></select>
      <button type="submit">Save</button>
    </form>`,
})
export class UserFormComponent {
  constructor(private http: HttpClient) {}
  save() { this.http.post("/api/users", this.form.value).subscribe(); }
}
""")
    assert entity(out, "Screen", name="src/app/users/user-form.component.ts")["attributes"]["framework"] == "angular"
    assert entity(out, "UIControl", name="email")["attributes"]["binds"] == "email"
    assert entity(out, "UIControl", name="role")["attributes"]["control_type"] == "select"
    assert entity(out, "Form", name="form1")["attributes"]["submits"] == ["POST /api/users"]


# ------------------------------------------------------------------ design tokens


def test_css_custom_properties_are_tokens():
    long = "linear-gradient(" + ", ".join(f"rgba(0, 0, 0, 0.{i})" for i in range(1, 9)) + ")"
    out = run("src/styles/tokens.css", f""":root {{
  --color-primary: #1f6feb;
  --space-4: 16px;
  --radius-md: 8px;
  --font-body: Inter, sans-serif;
  --shadow-card: 0 1px 3px rgba(0, 0, 0, .1);
  --hero: {long};
  --hero-image: url("https://cdn.example.com/a.png");
}}
[data-theme="dark"] {{ --color-primary: #58a6ff; }}
body {{ color: var(--color-primary); }}
""")
    tokens = {e["natural_key"]["name"]: e for e in out["entities"] if e["kind"] == "StyleToken"}
    assert set(tokens) == {"--color-primary", "--space-4", "--radius-md", "--font-body", "--shadow-card", "--hero", "--hero-image"}
    assert {n: t["attributes"]["category"] for n, t in tokens.items() if n in ("--color-primary", "--space-4", "--radius-md", "--font-body", "--shadow-card")} == {
        "--color-primary": "color", "--space-4": "spacing", "--radius-md": "radius", "--font-body": "font", "--shadow-card": "shadow"}
    primary = tokens["--color-primary"]
    assert primary["natural_key"]["system"] == "src/styles/tokens.css"
    assert primary["attributes"]["value"] == "#1f6feb" and primary["attributes"]["declarations"] == 2
    assert "value" not in tokens["--hero"]["attributes"]  # longer than 64 characters: the name only
    assert "value" not in tokens["--hero-image"]["attributes"]  # a URL is not a design value
    assert out["constructs"]["web:token-reference"] == (1, 0)


def test_scss_and_less_variables_are_tokens_at_top_level_only():
    scss = run("src/styles/_variables.scss", """$brand: #1f6feb;
$gutter: 16px !default;
@mixin card { $local: 4px; padding: $local; }
.x { $inner: 2px; margin: $gutter; }
""")
    assert names(scss, "StyleToken") == ["$brand", "$gutter"]
    assert entity(scss, "StyleToken", name="$brand")["attributes"]["platform_kind"] == "web:ScssVariable"
    assert scss["constructs"]["web:scss-mixin"] == (1, 0)
    less = run("src/styles/vars.less", "@primary: #333;\n@media (min-width: 600px) { .a { color: @primary; } }\n")
    assert names(less, "StyleToken") == ["@primary"]
    assert less["constructs"]["web:media-query"] == (1, 0)


@pytest.mark.parametrize("name, value, category", [
    ("--brand", "#fff", "color"), ("--bg", "rgb(0 0 0)", "color"), ("$text-color", "inherit", "color"),
    ("--gap", "8px", "spacing"), ("--radii-lg", "12px", "radius"), ("--font-size-sm", "12px", "size"),
    ("--font-mono", "monospace", "font"), ("--elevation-shadow", "0 0 2px #000", "shadow"),
    ("--breakpoint-md", "768px", "size"), ("--z-modal", "100", "other"),
])
def test_token_categories(name, value, category):
    assert styles.category(name, value) == category


def test_theme_objects_are_tokens_and_their_uses_style_the_screen():
    theme = run("src/theme.ts", """
export const theme = {
  colors: { primary: "#1f6feb", text: "#1f2328" },
  space: [0, 4, 8, 16],
  radii: { md: "8px" },
  apiBase: process.env.API_URL,
};
""")
    assert "theme.colors.primary" in names(theme, "StyleToken") and "theme.space.3" in names(theme, "StyleToken")
    assert "theme.apiBase" not in names(theme, "StyleToken")  # not a literal
    assert entity(theme, "StyleToken", name="theme.radii.md")["attributes"]["category"] == "radius"
    page = run("src/pages/Orders.tsx", """
import { theme } from "../theme";
export default function Orders() {
  return <a href="/orders" style={{ color: theme.colors.primary }}>Orders</a>;
}
""")
    assert ("src/pages/Orders.tsx", "theme.colors.primary?") in edges(page, "STYLED_BY")


def test_stylesheet_imports_style_the_screen():
    out = run("src/pages/Orders.tsx", """
import styles from "../styles/orders.module.scss";
import "../styles/tokens.css";
export default function Orders() { return <a className={styles.link} href="/orders" style={{ color: "var(--color-primary)" }}>All</a>; }
""")
    view = entity(out, "Screen", name="src/pages/Orders.tsx")["attributes"]
    assert view["stylesheets"] == ["src/styles/orders.module.scss", "src/styles/tokens.css"]
    systems = {r["object"]["natural_key"]["system"] for r in out["relations"] if r["predicate"] == "STYLED_BY"}
    assert systems == {"src/styles/orders.module.scss", "src/styles/_orders.module.scss", "src/styles/tokens.css"}
    assert out["constructs"]["web:css-module"] == (1, 1)


# ------------------------------------------------------------------ the fixture


def _fixture_files():
    base = FIXTURES_DIR / "F22-web-frontend" / "base"
    return sorted((str(p.relative_to(base)), p.read_bytes()) for p in base.rglob("*") if p.is_file())


def test_every_relation_on_the_fixture_fits_the_ontology():
    terms = {t.name: t.core for t in PACK.terms}
    count = 0
    for path, data in _fixture_files():
        for e in (e for e in PACK.extractors if e.matches(path)):
            result = e.extract(path, data, BLOB)
            declared = {}
            for o in result.observations:
                if o.kind == "entity":
                    p = o.payload
                    assert terms[p["attributes"]["platform_kind"]].value == p["kind"]
                    assert p["attributes"]["platform"] == "web"
                    for v in p["attributes"].values():
                        assert not isinstance(v, str) or len(v) <= 200, (path, v)
            for o in result.observations:
                if o.kind != "relation":
                    continue
                p = o.payload
                # "@symbol:<name>" is the Symbol the linker resolves, or the package it falls back to (section 8.5)
                special = {"@package": EntityKind.PACKAGE, "@file": EntityKind.ARTIFACT}
                if isinstance(p["subject"], str) and p["subject"].startswith("@symbol:"):
                    for kind in (EntityKind.SYMBOL, EntityKind.PACKAGE):
                        assert check_relation(p["predicate"], kind, EntityKind(p["object"]["kind"])) is None, (path, p)
                    subject = EntityKind.SYMBOL
                else:
                    subject = special[p["subject"]] if isinstance(p["subject"], str) else EntityKind(p["subject"]["kind"])
                assert check_relation(p["predicate"], subject, EntityKind(p["object"]["kind"])) is None, (path, p)
                assert o.evidence_class in ("E2", "E3")
                if p["object"].get("declare") is False and p["object"]["natural_key"].get("system") != path:
                    assert o.evidence_class == "E3", (path, p)  # resolved against another file
                count += 1
    assert count > 100


def _relative(path):
    return bool(path) and not path.startswith(("/", "./")) and ".." not in path.split("/") and ":" not in path


def test_screen_names_and_token_systems_are_repository_relative_paths():
    # A Screen is named by its route ("/orders") or by its path from the repository root;
    # a StyleToken's system is the stylesheet or module path from the repository root.
    seen = {"page_or_view": 0, "token": 0}
    for path, data in _fixture_files():
        for e in (e for e in PACK.extractors if e.matches(path)):
            for o in e.extract(path, data, BLOB).observations:
                p = o.payload
                if o.kind == "entity" and p["kind"] == "Screen":
                    if p["attributes"]["platform_kind"] == "web:Route":
                        assert p["natural_key"]["name"].startswith("/"), p
                    else:
                        assert p["natural_key"]["name"] == path and _relative(path), p
                        seen["page_or_view"] += 1
                elif o.kind == "entity" and p["kind"] == "StyleToken":
                    assert p["natural_key"]["system"] == path and _relative(path), p
                    seen["token"] += 1
                elif o.kind == "relation" and isinstance(p["object"], dict):
                    key = p["object"]["natural_key"]
                    if p["object"]["kind"] == "StyleToken":
                        assert _relative(key["system"]), p
                    elif p["object"]["kind"] == "Screen" and not key["name"].startswith("/"):
                        assert _relative(key["name"]), p
    assert seen["page_or_view"] and seen["token"]


def test_extraction_depends_only_on_the_file_bytes():
    for path, data in _fixture_files():
        for e in (e for e in PACK.extractors if e.matches(path)):
            a, b = e.extract(path, data, BLOB), e.extract(path, data, BLOB)
            assert [o.payload for o in a.observations] == [o.payload for o in b.observations]


def test_no_secret_shaped_value_is_copied():
    out = run("src/config/theme.ts", """
export const theme = {
  colors: { primary: "#000" },
  keys: { stripe: "<KEY>" },
};
""".replace("<KEY>", "sk_" + "live_4eC39HqLyjWDarjtT1zdp7dcAAAABBBBCCCC"))  # built here so the source holds no key-shaped literal
    blob = repr(out["entities"])
    assert "sk_live" not in blob


def test_constructs_with_no_neutral_form_are_counted_not_modelled():
    out = run("src/main.tsx", """
import { createPortal } from "react-dom";
import { create } from "zustand";
navigator.serviceWorker.register("/sw.js");
const socket = new WebSocket("wss://example.com/live");
const worker = new Worker(new URL("./worker.ts", import.meta.url));
localStorage.setItem("seen", "1");
customElements.define("x-badge", Badge);
export const useCart = create(() => ({ items: [] }));
export function Modal() { return createPortal(<div dangerouslySetInnerHTML={{ __html: html }} />, document.body); }
""")
    for construct in ("web:service-worker", "web:websocket", "web:web-worker", "web:browser-storage", "web:custom-element", "web:state-store", "web:portal", "web:raw-html"):
        seen, modelled = out["constructs"].get(construct, (0, 0))
        assert seen >= 1 and modelled == 0, construct
    assert names(out, "Screen") == []


def test_nuxt_pages_name_their_route_and_roles():
    out = run("pages/account/settings.vue", """<template>
  <form @submit.prevent="save"><input v-model="name" /><button>Save</button></form>
</template>
<script setup>
definePageMeta({ middleware: ["auth"], roles: ["customer"] });
async function save() { await $fetch("/api/account", { method: "PUT" }); }
</script>
""")
    assert names(out, "Screen") == ["/account/settings"]
    assert out["constructs"]["web:nuxt-page"] == (1, 1)
    assert edges(out, "REQUIRES_ROLE") == [("/account/settings", "customer")]
    assert entity(out, "Form", name="form1")["attributes"]["submits"] == ["PUT /api/account"]


# ------------------------------------------------------------------ wave 2 (section 8.5)


def test_the_pack_declares_model_fields_and_its_version():
    term = PACK.term("web:ModelField")
    assert term.core is EntityKind.FIELD and term.fidelity == "lossy"
    assert "web:form-model" in {c.id for c in PACK.constructs}
    assert PACK.version == "1.3.0"


def _git_repo(path: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(path), *cmd], check=True, env=env)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def _compile(tmp_path: Path, files: dict[str, str]):
    """Compile a one-commit repository: (entities by id, assertions)."""
    from repo_intel.compile.pipeline import compile_commit, load_shards
    from repo_intel.storage.store import ObjectStore

    repo = tmp_path / "repo"
    sha = _git_repo(repo, files)
    store = ObjectStore(tmp_path / "store", "t")
    result = compile_commit(store, repo, "github.com/1001", sha)
    shards = load_shards(store, result.manifest)
    return {e["id"]: e for s in shards.values() for e in s["entities"]}, [a for s in shards.values() for a in s["assertions"]]


def _renders(ents, assertions):
    """{screen name: (subject kind, subject key, qualifiers)} for every RENDERS assertion."""
    out = {}
    for a in assertions:
        if a["predicate"] == "RENDERS":
            s, o = ents[a["subject"]], ents[a["object"]]
            out[o["natural_key"]["name"]] = (s["kind"], s["natural_key"], a["qualifiers"])
    return out


RENDER_FILES = {
    "package.json": '{"name": "shop"}',
    "src/App.tsx": """
import { Route, Routes } from "react-router-dom";
import OrdersPage from "./pages/OrdersPage";
export function App() {
  return <Routes><Route path="/orders" element={<OrdersPage />} /></Routes>;
}
""",
    "src/pages/OrdersPage.tsx": """
export default function OrdersPage() {
  return <form><input name="q" /><a href="/orders">All</a></form>;
}
""",
    "src/pages/Hidden.tsx": """
const Hidden = () => <form><input name="q" /></form>;
export default Hidden;
""",
}


def test_a_view_is_rendered_by_the_symbol_its_file_exports(tmp_path):
    ents, assertions = _compile(tmp_path, RENDER_FILES)
    renders = _renders(ents, assertions)
    kind, key, qualifiers = renders["src/pages/OrdersPage.tsx"]
    assert kind == "Symbol" and key == {"package": "shop", "qualified_name": "src/pages/OrdersPage#OrdersPage"}
    assert "component" not in qualifiers
    symbol = next(e for e in ents.values() if e["kind"] == "Symbol" and e["natural_key"] == key)
    assert symbol is not None


def test_a_symbol_end_the_file_does_not_export_falls_back_to_the_package(tmp_path):
    ents, assertions = _compile(tmp_path, RENDER_FILES)
    renders = _renders(ents, assertions)
    # App.tsx names OrdersPage but does not export it: the package renders the route.
    kind, key, qualifiers = renders["/orders"]
    assert kind == "Package" and key["name"] == "shop" and qualifiers["component"] == "OrdersPage"
    # Hidden is exported only as `default`, so no exported symbol carries its name.
    kind, key, qualifiers = renders["src/pages/Hidden.tsx"]
    assert kind == "Package" and qualifiers["component"] == "Hidden"
    assert all(a["subject"] in ents and a["object"] in ents for a in assertions if a["predicate"] == "RENDERS")


TWO_ROUTERS = """
import { createBrowserRouter } from "react-router-dom";
import Home from "./Home";
import Admin from "./Admin";
import Users from "./Users";
export const shopRouter = createBrowserRouter([{ path: "/", element: <Home /> }]);
export const adminRouter = createBrowserRouter([
  { path: "/", element: <Admin /> },
  { path: "/users", element: <Users /> },
]);
"""


def test_a_file_with_two_routers_keys_its_screens_by_app():
    out = run("src/routers.tsx", TWO_ROUTERS)
    keys = sorted((e["natural_key"]["name"], e["natural_key"].get("app")) for e in out["entities"] if e["kind"] == "Screen")
    assert keys == [("/", "adminRouter"), ("/", "shopRouter"), ("/users", "adminRouter")]
    assert entity(out, "Screen", name="/users")["attributes"]["app"] == "adminRouter"
    assert any("declares 2 routers" in d for d in out["diagnostics"])


def test_a_single_router_keeps_its_screen_keys():
    out = run("src/App.tsx", REACT_ROUTES)
    assert all(set(e["natural_key"]) == {"platform", "name"} for e in out["entities"] if e["kind"] == "Screen")


def test_two_router_components_in_one_file_are_told_apart():
    out = run("src/Apps.tsx", """
import { BrowserRouter, MemoryRouter, Route, Routes } from "react-router-dom";
import A from "./A";
import B from "./B";
export function ShopApp() { return <BrowserRouter><Routes><Route path="/" element={<A />} /></Routes></BrowserRouter>; }
export function AdminApp() { return <MemoryRouter><Routes><Route path="/" element={<B />} /></Routes></MemoryRouter>; }
""")
    assert sorted(e["natural_key"]["app"] for e in out["entities"] if e["kind"] == "Screen") == ["AdminApp", "ShopApp"]


def test_nested_routes_elements_under_one_router_share_the_app():
    # <Routes> blocks in several components render under the one router the app mounts.
    out = run("src/Sections.tsx", """
import { Route, Routes } from "react-router-dom";
import A from "./A";
import B from "./B";
export function Shop() { return <Routes><Route path="/shop" element={<A />} /></Routes>; }
export function Admin() { return <Routes><Route path="/admin" element={<B />} /></Routes>; }
""")
    assert all("app" not in e["natural_key"] for e in out["entities"] if e["kind"] == "Screen")


def _imports(out):
    return sorted((r["subject"], r["object"]["natural_key"]["path"], r["qualifiers"]["via"], r["evidence_class"])
                  for r in out["relations"] if r["predicate"] == "DEPENDS_ON")


def test_stylesheet_imports_are_artifact_dependencies():
    out = run("src/styles/main.scss", '@use "variables";\n@import "./reset.css";\n@forward "mixins/buttons";\n@import url("https://fonts.example.com/a.css");\n$x: 1px;\n')
    found = _imports(out)
    assert ("@file", "src/styles/_variables.scss", "@use", "E3") in found
    assert ("@file", "src/styles/reset.css", "@import", "E3") in found
    assert any(p.startswith("src/styles/mixins/") and via == "@forward" for _, p, via, _ in found)
    assert not any("example.com" in p for _, p, _, _ in found)  # another host is not a file of this unit
    assert check_relation("DEPENDS_ON", EntityKind.ARTIFACT, EntityKind.ARTIFACT) is None


def test_a_vue_style_block_import_is_a_dependency_too():
    out = run("src/views/A.vue", '<template><div /></template>\n<style lang="scss">\n@use "../styles/variables";\n</style>\n')
    assert ("@file", "src/styles/_variables.scss", "@use", "E3") in _imports(out)


def _aliases(out):
    return sorted((r["subject"]["natural_key"]["name"], r["object"]["natural_key"]["system"], r["object"]["natural_key"]["name"], r["evidence_class"])
                  for r in out["relations"] if r["predicate"] == "ALIAS_OF")


def test_css_scss_and_less_token_aliases():
    css = run("src/a.css", '@import "./base.css";\n:root { --a: #fff; --b: var(--a); --c: var(--brand); --d: calc(var(--a) * 2); --e: var(--a, red); }\n')
    assert _aliases(css) == [("--b", "src/a.css", "--a", "E2"), ("--c", "src/base.css", "--brand", "E3")]
    scss = run("src/b.scss", "$a: #fff;\n$b: $a;\n$c: darken($a, 10%);\n$d: colors.$brand;\n")
    assert ("$b", "src/b.scss", "$a", "E2") in _aliases(scss)
    assert not any(name == "$c" for name, *_ in _aliases(scss))  # a computed value is not an alias
    less = run("src/c.less", "@a: #fff;\n@b: @a;\n")
    assert _aliases(less) == [("@b", "src/c.less", "@a", "E2")]


def test_an_alias_to_an_undeclared_token_without_imports_relates_nothing():
    assert _aliases(run("src/a.css", ":root { --b: var(--nowhere); }\n")) == []


def test_theme_token_aliases():
    out = run("src/darkTheme.ts", """
import { theme } from "./theme";
export const palette = { blue: "#1f6feb" };
export const darkTheme = { colors: { primary: palette.blue, brand: theme.colors.primary, other: somewhere.blue } };
""")
    found = _aliases(out)
    assert ("darkTheme.colors.primary", "src/darkTheme.ts", "palette.blue", "E2") in found
    assert any(name == "darkTheme.colors.brand" and system.startswith("src/theme") and target == "theme.colors.primary" and cls == "E3"
               for name, system, target, cls in found)
    assert not any(name == "darkTheme.colors.other" for name, *_ in found)


def test_token_aliases_fit_the_ontology():
    assert check_relation("ALIAS_OF", EntityKind.STYLE_TOKEN, EntityKind.STYLE_TOKEN) is None


def _fields(out):
    return sorted((e["natural_key"]["table"], e["natural_key"]["name"], e["attributes"]["schema"]) for e in out["entities"] if e["kind"] == "Field")


def _binds(out):
    return sorted((r["subject"]["natural_key"]["name"], r["object"]["natural_key"]["name"]) for r in out["relations"] if r["predicate"] == "BINDS_TO")


def test_react_hook_form_controls_bind_the_fields_of_a_typescript_interface():
    out = run("src/NewOrder.tsx", """
import { useForm } from "react-hook-form";
interface NewOrder { customer: string; quantity: number; address: { city: string } }
export function NewOrder() {
  const { register } = useForm<NewOrder>();
  return <form><input {...register("customer")} /><input {...register("address.city")} /><input {...register("nickname")} /></form>;
}
""")
    assert _fields(out) == [("src/NewOrder.tsx#NewOrder", n, "typescript-interface") for n in ("address", "address.city", "customer", "quantity")]
    assert _binds(out) == [("address.city", "address.city"), ("customer", "customer")]
    assert entity(out, "Field", name="customer")["attributes"]["platform_kind"] == "web:ModelField"
    assert any("nickname" in d and "does not declare" in d for d in out["diagnostics"])
    assert out["constructs"]["web:form-model"] == (1, 1)
    assert check_relation("BINDS_TO", EntityKind.UI_CONTROL, EntityKind.FIELD) is None


def test_a_zod_schema_through_a_resolver_is_a_form_model():
    out = run("src/Signup.tsx", """
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
const signup = z.object({ email: z.string().email(), age: z.number().optional() }).strict();
export function Signup() {
  const { register } = useForm({ resolver: zodResolver(signup) });
  return <form><input {...register("email")} /></form>;
}
""")
    assert _fields(out) == [("src/Signup.tsx#signup", "age", "zod"), ("src/Signup.tsx#signup", "email", "zod")]
    assert _binds(out) == [("email", "email")]


def test_a_yup_schema_on_formik_is_a_form_model():
    out = run("src/Contact.tsx", """
import { Formik, Form, Field } from "formik";
import * as Yup from "yup";
const contactSchema = Yup.object().shape({ name: Yup.string().required(), topic: Yup.string() });
export function Contact() {
  return <Formik validationSchema={contactSchema} onSubmit={send}><Form><Field name="name" /><Field name="topic" /></Form></Formik>;
}
""")
    assert _fields(out) == [("src/Contact.tsx#contactSchema", "name", "yup"), ("src/Contact.tsx#contactSchema", "topic", "yup")]
    assert _binds(out) == [("name", "name"), ("topic", "topic")]


def test_a_typed_vue_model_binds_its_fields():
    out = run("src/views/Checkout.vue", """<template>
  <form @submit.prevent="save"><input v-model="form.email" /><input v-model="form.coupon" /></form>
</template>
<script setup lang="ts">
import { reactive } from "vue";
type Checkout = { email: string; coupon: string };
const form = reactive<Checkout>({ email: "", coupon: "" });
</script>
""")
    assert _fields(out) == [("src/views/Checkout.vue#Checkout", "coupon", "typescript-type"), ("src/views/Checkout.vue#Checkout", "email", "typescript-type")]
    assert _binds(out) == [("form.coupon", "coupon"), ("form.email", "email")]


def test_an_untyped_model_declares_no_field():
    out = run("src/views/Checkout.vue", """<template>
  <form><input v-model="form.email" /></form>
</template>
<script setup>
import { reactive } from "vue";
const form = reactive({ email: "" });
</script>
""")
    assert _fields(out) == [] and _binds(out) == []
    assert entity(out, "UIControl", name="form.email")["attributes"]["binds"] == "form.email"


def test_a_model_this_file_does_not_declare_or_list_literally_declares_no_field():
    imported = run("src/A.tsx", """
import { useForm } from "react-hook-form";
import type { Order } from "./types";
export function A() { const { register } = useForm<Order>(); return <form><input {...register("id")} /></form>; }
""")
    assert _fields(imported) == [] and _binds(imported) == []
    assert any("Order is not declared in this file" in d for d in imported["diagnostics"])
    assert imported["constructs"]["web:form-model"] == (1, 0)
    extended = run("src/B.tsx", """
import { useForm } from "react-hook-form";
interface Base { id: string }
interface Order extends Base { note: string }
export function B() { const { register } = useForm<Order>(); return <form><input {...register("note")} /></form>; }
""")
    assert _fields(extended) == [] and _binds(extended) == []
    assert any("does not list all its fields literally" in d for d in extended["diagnostics"])
    spread = run("src/C.tsx", """
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
const c = z.object({ ...base, note: z.string() });
export function C() { const { register } = useForm({ resolver: zodResolver(c) }); return <form><input {...register("note")} /></form>; }
""")
    assert _fields(spread) == []


ACCESSIBLE = """<!doctype html>
<html><body>
<header><nav><a href="/">Home</a></nav></header>
<main>
  <h1>Contact</h1>
  <h3>Form</h3>
  <form name="contact">
    <label for="email">Email</label><input id="email" name="email" type="email" />
    <label>Topic <select name="topic"><option>a</option></select></label>
    <span id="msg-label">Message</span><textarea name="message" aria-labelledby="msg-label"></textarea>
    <input name="phone" placeholder="Phone" tabindex="2" />
    <input name="fax" />
    <input type="checkbox" name="agree" aria-label="I agree" />
    <div role="button" name="custom">Send</div>
    <button type="submit">Send</button>
  </form>
</main>
<footer>Shop</footer>
</body></html>
"""


def test_controls_carry_their_accessible_name_source_and_role():
    out = run("public/contact.html", ACCESSIBLE)
    facts = {e["natural_key"]["name"]: e["attributes"]["accessibility"] for e in out["entities"] if e["kind"] == "UIControl" and e["natural_key"]["container"].endswith("#contact")}
    assert facts["email"] == {"name_source": "label-for", "role": "textbox"}
    assert facts["topic"]["name_source"] == "wrapping-label" and facts["topic"]["role"] == "combobox"
    assert facts["message"]["name_source"] == "aria-labelledby"
    assert facts["phone"] == {"name_source": "placeholder", "role": "textbox", "tabindex": 2}
    assert facts["fax"]["name_source"] == "none"
    assert facts["agree"] == {"name_source": "aria-label", "role": "checkbox"}
    assert facts["submit"] == {"name_source": "content", "role": "button"}


def test_screens_carry_landmarks_headings_and_tabindex():
    out = run("public/contact.html", ACCESSIBLE)
    facts = entity(out, "Screen", name="public/contact.html")["attributes"]["accessibility"]
    assert facts["landmarks"] == ["banner", "contentinfo", "main", "navigation"]
    assert facts["headings"] == [1, 3] and facts["heading_skips"] == 1
    assert facts["positive_tabindex"] == 1


def test_a_bound_name_counts_but_its_value_is_never_copied():
    out = run("src/A.tsx", 'export function A() { return <form><input name="q" aria-label={t("search")} tabIndex={n} /></form>; }')
    facts = entity(out, "UIControl", name="q")["attributes"]["accessibility"]
    assert facts["name_source"] == "aria-label" and facts["tabindex"] == "dynamic"
    assert "search" not in repr(facts)


def test_contrast_ratios_of_colour_tokens():
    from repo_intel.platforms.web import accessibility

    assert accessibility.contrast_ratio(accessibility.parse_colour("#000"), accessibility.parse_colour("#ffffff")) == 21.0
    assert accessibility.parse_colour("rgb(255 0 0)") == accessibility.parse_colour("#f00")
    assert accessibility.parse_colour("var(--x)") is None
    out = run("src/buttons.css", """:root { --fg: #ffffff; --bg: #1f6feb; --muted: #8c959f; --alias-bg: var(--bg); }
.a { color: var(--fg); background-color: var(--bg); }
.b { color: var(--muted); background: var(--fg); }
.c { color: var(--fg); background-color: var(--alias-bg); }
.d { color: var(--fg); background-color: var(--nowhere); }
""")
    fg = entity(out, "StyleToken", name="--fg")["attributes"]["contrast"]
    assert fg == [{"background": "--alias-bg", "ratio": 4.63, "selector": ".c"}, {"background": "--bg", "ratio": 4.63, "selector": ".a"}]
    assert entity(out, "StyleToken", name="--muted")["attributes"]["contrast"] == [{"background": "--fg", "ratio": 3.04, "selector": ".b"}]


def test_a_token_declared_twice_has_no_contrast():
    out = run("src/t.css", ":root { --fg: #fff; --bg: #000; }\n[data-theme=dark] { --fg: #000; }\n.a { color: var(--fg); background-color: var(--bg); }\n")
    assert not entity(out, "StyleToken", name="--fg")["attributes"].get("contrast")


def test_accessibility_coverage_counts_named_controls():
    from repo_intel.platforms.web.accessibility import accessibility_coverage

    out = run("public/contact.html", ACCESSIBLE)
    css = run("src/buttons.css", ":root { --fg: #fff; --muted: #8c959f; }\n.b { color: var(--muted); background: var(--fg); }\n")
    entries = accessibility_coverage({"a": {"entities": out["entities"] + css["entities"]}})
    assert len(entries) == 1
    e = entries[0]
    controls = [x for x in out["entities"] if x["kind"] == "UIControl"]
    unnamed = sum(x["attributes"]["accessibility"]["name_source"] == "none" for x in controls)
    assert e["domain"] == "accessibility" and e["scope"] == "web"
    assert (e["numerator"], e["denominator"]) == (len(controls) - unnamed, len(controls))
    assert e["detail"]["screens_with_heading_skips"] == 1 and e["detail"]["screens_with_positive_tabindex"] == 1
    assert e["detail"]["contrast_below_4_5"] == ["src/buttons.css --muted on --fg"]
    assert accessibility_coverage({}) == []
