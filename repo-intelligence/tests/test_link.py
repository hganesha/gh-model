"""Cross-file linking: graded resolution inside a unit, contract matching across units."""

import pytest

from conftest import REPO_ID
from repo_intel.compile.contracts import Evidence, Span, entity_id
from repo_intel.compile.fixtures import load_golden, materialize
from repo_intel.compile.link import _Linker, match_call, normalize_route
from repo_intel.compile.pipeline import compile_commit, load_shards
from repo_intel.storage.store import ObjectStore


@pytest.fixture(scope="module")
def graphs(tmp_path_factory):
    def build(name):
        root = tmp_path_factory.mktemp(name)
        shas = materialize(name, root / "repo")
        store = ObjectStore(root / "store", "t")
        out = []
        for sha in shas:
            m = compile_commit(store, root / "repo", REPO_ID, sha).manifest
            shards = load_shards(store, m)
            ents = {e["id"]: e for s in shards.values() for e in s["entities"]}
            out.append({
                "entities": ents,
                "edges": {(ents[a["subject"]]["name"], a["predicate"], ents[a["object"]]["name"]): a for s in shards.values() for a in s["assertions"]},
                "unknowns": [(ents[u["subject"]]["name"], u["predicate"], u["reason"]) for u in store.get_json(m.unknowns_ref)],
                "conflicts": store.get_json(m.conflicts_ref),
                "coverage": {(c["domain"], c["scope"]): c for c in store.get_json(m.coverage_ref)},
            })
        return out
    cache = {}
    return lambda name: cache.setdefault(name, build(name))


def test_routes_resolve_to_handler_symbols(graphs):
    g = graphs("F01-python-api")[0]
    for handler, endpoint in [("list_customers", "GET /customers"), ("create_customer", "POST /customers")]:
        a = g["edges"][(handler, "IMPLEMENTS", endpoint)]
        assert (a["resolution"], a["evidence_class"], a["epistemic_status"], a["assertion_kind"]) == ("exact", "E3", "supported", "derived_relation")
        assert len(a["evidence_ids"]) == 2, "cites the route declaration and the handler definition"
    assert g["coverage"][("route_to_handler", ".")]["numerator"] == 2


def test_orm_call_sites_link_to_tables_across_files(graphs):
    g = graphs("F01-python-api")[0]
    assert g["edges"][("list_customers", "READS_FROM", "customers")]["resolution"] == "exact"
    assert g["edges"][("create_customer", "WRITES_TO", "customers")]["qualifiers"] == {"via": "orm"}
    assert ("create_customer", "READS_FROM", "customers") not in g["edges"]


def test_f09_golden_data_edges_and_dynamic_sql_stays_unknown(graphs):
    g = graphs("F09-data-heavy")[0]
    golden = load_golden("F09-data-heavy")
    eid = lambda item: entity_id(REPO_ID, item["kind"], item["natural_key"])
    for edge in (e for e in golden["edges"] if e["subject"]["kind"] == "Symbol"):
        assert eid(edge["subject"]) in g["entities"], "golden Symbol key (package + qualified name) must match"
        assert any(a["subject"] == eid(edge["subject"]) and a["object"] == eid(edge["object"]) and a["predicate"] == edge["predicate"] for a in g["edges"].values())
    assert ("count_rows", "READS_FROM", "dynamic_sql: the statement is built at runtime") in g["unknowns"]
    assert not any(k[0] == "count_rows" and k[1] in ("READS_FROM", "WRITES_TO") for k in g["edges"]), "dynamic SQL is never resolved to a table"
    cov = g["coverage"][("data_access", ".")]
    assert (cov["numerator"], cov["denominator"]) == (2, 3) and cov["detail"]["dynamic_sql"] == 1


def test_test_links_are_candidates_not_proof(graphs):
    a = graphs("F01-python-api")[0]["edges"][("create_customer", "TESTED_BY", "test_handlers_exist")]
    assert (a["resolution"], a["evidence_class"], a["epistemic_status"]) == ("candidate", "E4", "inferred")


def test_contract_matches_implementation_and_conflict_opens_when_contract_runs_ahead(graphs):
    commits = graphs("F03-python-ts-hybrid")
    assert ("Catalog API", "CONTAINS", "GET /products") in commits[0]["edges"]
    assert commits[0]["conflicts"] == [] and commits[1]["conflicts"] == []
    assert ("Catalog API", "CONTAINS", "GET /products/{product_id}") in commits[1]["edges"]
    opened = commits[2]["conflicts"]
    assert len(opened) == 1 and opened[0]["conflict_type"] == "declared_vs_implemented" and "/reviews" in opened[0]["basis"]
    assert opened[0]["evidence_ids"] and opened[0]["subject_ids"]
    cov = commits[2]["coverage"][("contract_match", "@repo")]
    assert (cov["numerator"], cov["denominator"]) == (2, 3)
    assert not any(e["kind"] == "Endpoint" and "reviews" in e["name"] for e in commits[2]["entities"].values()), "no implementation is invented"


def test_orphan_contract_conflict_closes_when_contract_is_removed(graphs):
    commits = graphs("F10-ambiguous-incomplete")
    contract = [[x for x in c["conflicts"] if x["conflict_type"] == "declared_vs_implemented"] for c in commits]
    assert [len(c) for c in contract] == [1, 1, 1, 0]
    assert "GET /users" in contract[0][0]["basis"]


def test_http_consumers_are_candidates_and_prefer_get(graphs):
    g = graphs("F02-ts-monorepo")[0]
    a = g["edges"][("@acme/web", "CONSUMES", "GET /orders")]
    assert (a["resolution"], a["epistemic_status"], a["qualifiers"]) == ("candidate", "inferred", {"via": "http"})
    assert ("@acme/web", "CONSUMES", "POST /orders") not in g["edges"]
    proxied = graphs("F03-python-ts-hybrid")[0]["edges"]
    assert ("catalog-frontend", "CONSUMES", "GET /products") in proxied  # /api/products behind a dev proxy


def test_route_normalisation():
    assert normalize_route("/orders/:id") == normalize_route("/orders/{order_id}") == normalize_route("orders/[id]/") == "/orders/{}"
    assert match_call("{}/orders", "/orders") and match_call("/api/products/1?x=1", "/products/1")
    assert not match_call("/health", "/") and not match_call("/orders", "/orders/{id}")


# ---- synthetic units: import grading


def _ev(n):
    return Evidence.make("ab" * 32, Span(start_byte=n, end_byte=n + 1, start_line=1, end_line=1), "x").row()


def _summary(path, *observations):
    return {"path": path, "observations": [{"kind": k, "payload": p, "evidence": _ev(i), "evidence_class": "E2"} for i, (k, p) in enumerate(observations)]}


def _imp(module, names=(), level=0, **extra):
    return ("import", {"module": module, "names": list(names), "level": level, **extra})


def test_python_import_grades():
    files = [
        _summary("svc/pyproject.toml", ("package_declaration", {"ecosystem": "pypi", "name": "svc", "manifest_dir": "svc"}), ("dependency_declaration", {"ecosystem": "pypi", "name": "pyyaml", "specifier": "", "scope": "runtime"})),
        _summary("svc/app/__init__.py"),
        _summary("svc/app/models.py", ("symbol", {"name": "User", "symbol_kind": "class"})),
        _summary("svc/src/extra/util.py", ("symbol", {"name": "slug", "symbol_kind": "function"})),
        _summary("svc/app/main.py",
                 _imp("models", ["User"], 1, aliases={"User": "U"}), _imp("", ["models"], 1), _imp("app.models", ["User"]), _imp("extra.util", ["slug"]),
                 _imp("os"), _imp("yaml"), _imp("sibling_pkg"), _imp("requests"), _imp("missing", ["x"], 1), _imp("app.nowhere")),
    ]
    linker = _Linker({"name": "svc", "paths": []}, files, [{"ecosystem": "pypi", "name": "sibling-pkg", "manifest_dir": "other"}])
    linker.resolve_imports()
    assert linker.imports == {"internal": 4, "workspace": 1, "external": 1, "stdlib": 1, "unresolved": 3}
    assert sorted(linker.unresolved_imports) == ["svc/app/main.py: .missing", "svc/app/main.py: app.nowhere", "svc/app/main.py: requests"]
    assert linker.lookup("svc/app/main.py", "U") == ("svc/app/models.py", "User"), "aliased import binds the local name"
    assert linker.lookup("svc/app/main.py", "slug") == ("svc/src/extra/util.py", "slug"), "src layout"
    assert linker.lookup("svc/app/main.py", "Nope") is None
    assert linker.qualified("svc/app/models.py", "User") == "app.models.User" and linker.qualified("svc/app/__init__.py", "x") == "app.x"


def test_typescript_import_grades():
    files = [
        _summary("web/package.json", ("package_declaration", {"ecosystem": "npm", "name": "web", "manifest_dir": "web"}),
                 ("dependency_declaration", {"ecosystem": "npm", "name": "react", "specifier": "19", "scope": "runtime"}),
                 ("dependency_declaration", {"ecosystem": "npm", "name": "@acme/shared", "specifier": "workspace:*", "scope": "runtime"})),
        _summary("web/src/lib/index.ts", ("symbol", {"name": "helper", "symbol_kind": "function"})),
        _summary("web/src/Button.tsx", ("symbol", {"name": "Button", "symbol_kind": "function"})),
        _summary("web/src/app.ts", *[("import", {"specifier": s, "names": n, "form": "import"}) for s, n in [
            ("./Button", ["Button"]), ("./lib", ["helper"]), ("react", ["default"]), ("react/jsx-runtime", []), ("@acme/shared", ["Order"]),
            ("node:fs", ["default"]), ("path", ["default"]), ("./gone", ["x"]), ("lodash", ["default"]), ("@acme/unlisted", ["y"])]]),
    ]
    workspace = [{"ecosystem": "npm", "name": "@acme/shared", "manifest_dir": "packages/shared"}, {"ecosystem": "npm", "name": "@acme/unlisted", "manifest_dir": "packages/unlisted"}]
    linker = _Linker({"name": "web", "paths": []}, files, workspace)
    linker.resolve_imports()
    assert linker.imports == {"internal": 2, "workspace": 1, "external": 2, "stdlib": 2, "unresolved": 3}
    assert linker.lookup("web/src/app.ts", "helper") == ("web/src/lib/index.ts", "helper"), "directory import resolves to index"
    assert any("@acme/unlisted" in u for u in linker.unresolved_imports), "a sibling package that is not a declared dependency is not resolved"
    assert linker.qualified("web/src/Button.tsx", "Button") == "src/Button#Button"


def test_tsconfig_path_aliases_resolve():
    from repo_intel.extractors.tsconfig import TsConfig, strip_jsonc
    raw = b'''{
  // comments and trailing commas are normal in tsconfig
  "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["./src/*"], "~config": ["./config/index.ts"], }, },
}'''
    found = TsConfig().extract("web/tsconfig.json", raw, "ab" * 32).observations
    assert found[0].kind == "ts_config"
    assert found[0].payload == {"dir": "web", "extends": [], "base_url": ".", "paths": {"@/*": ["./src/*"], "~config": ["./config/index.ts"]}}
    assert strip_jsonc('{"a": "// not a comment", /* x */ "b": [1,],}') == '{"a": "// not a comment",  "b": [1]}'
    assert TsConfig().extract("tsconfig.json", b"{ not json", "ab" * 32).parsed is False
    assert not TsConfig().matches("package.json") and TsConfig().matches("apps/web/tsconfig.app.json")

    files = [
        _summary("web/package.json", ("package_declaration", {"ecosystem": "npm", "name": "web", "manifest_dir": "web"})),
        _summary("web/tsconfig.json", ("ts_config", found[0].payload)),
        _summary("web/src/components/ui/button.tsx", ("symbol", {"name": "Button", "symbol_kind": "function"})),
        _summary("web/src/client/index.ts", ("symbol", {"name": "api", "symbol_kind": "const"})),
        _summary("web/config/index.ts"),
        _summary("web/src/page.tsx", *[("import", {"specifier": s, "names": n, "form": "import"}) for s, n in [
            ("@/components/ui/button", ["Button"]), ("@/client", ["api"]), ("~config", []), ("@/missing", ["x"]), ("./client/index.js", ["api"])]]),
    ]
    linker = _Linker({"name": "web", "paths": []}, files, [])
    linker.resolve_imports()
    assert linker.imports["internal"] == 4 and linker.unresolved_imports == ["web/src/page.tsx: @/missing"]
    assert linker.lookup("web/src/page.tsx", "Button") == ("web/src/components/ui/button.tsx", "Button")


def test_typescript_topic_through_an_imported_object_constant():
    files = [
        _summary("svc/package.json", ("package_declaration", {"ecosystem": "npm", "name": "svc", "manifest_dir": "svc"})),
        _summary("svc/src/topics.ts", ("string_constant", {"name": "Topics.SHIPPED", "value": "orders.shipped"})),
        _summary("svc/src/publisher.ts",
                 ("import", {"specifier": "./topics", "names": ["Topics"], "form": "import"}),
                 ("topic_use", {"role": "publish", "transport": "kafka", "name": "Topics.SHIPPED"}),
                 ("topic_use", {"role": "publish", "transport": "kafka", "name": "Topics.MISSING"})),
    ]
    linker = _Linker({"name": "svc", "paths": []}, files, [])
    linker.resolve_imports()
    assert linker.link_topics() == {"literal": 0, "constant": 1, "unresolved": 1}
    topics = [o["payload"]["name"] for o in linker.out if o["kind"] == "entity" and o["payload"]["kind"] == "EventTopic"]
    assert topics == ["orders.shipped"]
    assert [u["reason"] for u in linker.unknowns] == ["topic Topics.MISSING does not resolve to a string constant in this unit"]


# ---- GI-008: FastAPI router prefixes, call graph, method symbols


def _py(path, source):
    from repo_intel.extractors.python_source import PythonSource
    out = PythonSource().extract(path, source.encode(), "cd" * 32)
    return {"path": path, "observations": [o.model_dump(mode="json") for o in out.observations]}


def test_router_prefixes_compose_across_files_and_levels():
    files = [
        _summary("svc/pyproject.toml", ("package_declaration", {"ecosystem": "pypi", "name": "svc", "manifest_dir": "svc"})),
        _py("svc/app/__init__.py", ""),
        _py("svc/app/main.py", 'from fastapi import FastAPI\nfrom .api import v1\nfrom .api.v1 import loose\napp = FastAPI()\napp.include_router(v1.api, prefix="/api")\napp.include_router(loose.router, prefix=PREFIX)\n'),
        _py("svc/app/api/__init__.py", ""),
        _py("svc/app/api/v1.py", 'from fastapi import APIRouter\nfrom .items import router as items_router\napi = APIRouter(prefix="/v1")\napi.include_router(items_router)\napi.include_router(items_router, prefix="/legacy")\n'),
        _py("svc/app/api/items.py", 'from fastapi import APIRouter\nrouter = APIRouter(prefix="/items")\n\n@router.get("/{item_id}")\ndef get_item(item_id: int):\n    return {}\n'),
        _py("svc/app/api/v1/loose.py", ""),
        _py("svc/app/orphan.py", 'from fastapi import APIRouter\nrouter = APIRouter()\n\n@router.post("/ping")\ndef ping():\n    return {}\n'),
    ]
    linker = _Linker({"name": "svc", "paths": []}, files, [])
    linker.resolve_imports()
    found = linker.link_routers()
    routes = sorted(o["payload"]["natural_key"]["route"] for o in linker.out if o["kind"] == "entity")
    assert routes == ["/api/v1/items/{item_id}", "/api/v1/legacy/items/{item_id}", "/ping"]
    assert not any(r.startswith("/items") for r in routes), "a router's own path is not served"
    orphan = next(o for o in linker.out if o["kind"] == "entity" and o["payload"]["natural_key"]["route"] == "/ping")
    assert orphan["payload"]["attributes"]["mounted"] is False and orphan["evidence_class"] == "E2"
    composed = next(o for o in linker.out if o["kind"] == "entity" and o["payload"]["natural_key"]["route"] == "/api/v1/items/{item_id}")
    assert composed["evidence_class"] == "E3" and len(composed["evidence"]) == 3, "route plus each include on the way"
    assert linker.router_counts == {"composed": 2, "unmounted": 1, "dynamic_prefix": 0, "unknown_router": 0}
    counts = linker.link_routes(found)
    assert counts == {"symbol": 2, "inline": 0, "unresolved": 0}
    implemented = sorted(o["payload"]["object"]["name"] for o in linker.out if o["kind"] == "relation" and o["payload"]["predicate"] == "IMPLEMENTS")
    assert implemented == ["GET /api/v1/items/{item_id}", "GET /api/v1/legacy/items/{item_id}", "POST /ping"]


def test_python_call_graph_resolves_exact_targets_only():
    files = [
        _summary("svc/pyproject.toml", ("package_declaration", {"ecosystem": "pypi", "name": "svc", "manifest_dir": "svc"})),
        _py("svc/app/__init__.py", ""),
        _py("svc/app/util.py", "def fmt(x):\n    return x\n\nVALUE = make()\n"),
        _py("svc/app/models.py", "class Order:\n    def save(self):\n        return self._write() or self.inherited()\n\n    def _write(self):\n        return 1\n"),
        _py("svc/app/svc.py", "import json\nfrom . import util\nfrom .models import Order as O\nfrom .util import VALUE\n\ndef run(x):\n    util.fmt(x)\n    json.dumps(x)\n    VALUE()\n    o = O()\n    O.save(o)\n    run(x)\n    return util.missing(x)\n"),
    ]
    linker = _Linker({"name": "svc", "paths": []}, files, [])
    linker.resolve_imports()
    counts = linker.link_calls()
    edges = sorted((o["payload"]["subject"]["natural_key"]["qualified_name"], o["payload"]["object"]["natural_key"]["qualified_name"]) for o in linker.out if o["kind"] == "relation")
    assert edges == [
        ("app.models.Order.save", "app.models.Order._write"),
        ("app.svc.run", "app.models.Order"),
        ("app.svc.run", "app.models.Order.save"),
        ("app.svc.run", "app.util.fmt"),
    ]
    assert counts == {"resolved": 4, "unresolved": 2, "not_a_symbol": 1}, "util.missing and self.inherited are unresolved; VALUE is not a symbol"
    assert all(o["evidence_class"] == "E3" and o["resolution"] == "exact" for o in linker.out)


def test_f01_routers_are_served_under_their_prefixes(graphs):
    g = graphs("F01-python-api")[4]
    routes = {e["natural_key"]["route"] for e in g["entities"].values() if e["kind"] == "Endpoint"}
    assert routes == {"/customers", "/api/v1/orders/{customer_id}/total", "/admin/stats", "/stats"}
    assert not any(r.startswith("/orders") for r in routes)
    a = g["edges"][("customer_total", "IMPLEMENTS", "GET /api/v1/orders/{customer_id}/total")]
    assert (a["resolution"], a["evidence_class"]) == ("exact", "E3")
    assert g["edges"][("GET /stats", "ALIAS_OF", "GET /admin/stats")]["qualifiers"] == {"basis": "mounted_sub_application", "prefix": "/admin"}
    for caller, callee in [("customer_total", "load_customer"), ("customer_total", "OrderService"), ("OrderService.total", "OrderService._sum"), ("create_customer", "Customer")]:
        assert g["edges"][(caller, "CALLS", callee)]["resolution"] == "exact"
    assert g["edges"][("load_customer", "READS_FROM", "customers")]
    coverage = g["coverage"][("route_to_handler", ".")]
    assert coverage["detail"]["routers"]["composed"] == 2 and coverage["numerator"] == coverage["denominator"] == 4


# ---- GI-009: TypeScript linking


def _ts(path, source):
    from repo_intel.extractors.typescript_source import TypeScriptSource
    out = TypeScriptSource().extract(path, source.encode(), "cd" * 32)
    return {"path": path, "observations": [o.model_dump(mode="json") for o in out.observations]}


def test_workspace_imports_bind_to_the_sibling_package_entry_points():
    files = [
        _summary("apps/web/package.json", ("package_declaration", {"ecosystem": "npm", "name": "web", "manifest_dir": "apps/web"}),
                 *[("dependency_declaration", {"ecosystem": "npm", "name": n, "specifier": "workspace:*", "scope": "runtime"}) for n in ("@acme/ui", "@acme/shared", "@acme/legacy")]),
        _summary("apps/web/src/page.tsx", *[("import", {"specifier": s, "names": n, "form": "import"}) for s, n in [
            ("@acme/ui", ["Button"]), ("@acme/ui/button", ["Button"]), ("@acme/ui/internal", ["x"]), ("@acme/shared/types", ["Order"]), ("@acme/legacy", ["y"])]]),
    ]
    workspace = [
        {"ecosystem": "npm", "name": "@acme/ui", "manifest_dir": "packages/ui", "entries": {".": "packages/ui/src/index.ts", "./button": "packages/ui/src/button.tsx"}, "exports_strict": True},
        {"ecosystem": "npm", "name": "@acme/shared", "manifest_dir": "packages/shared", "entries": {".": "packages/shared/src/index.ts"}, "exports_strict": False},
        {"ecosystem": "npm", "name": "@acme/legacy", "manifest_dir": "packages/legacy"},
    ]
    linker = _Linker({"name": "apps/web", "paths": []}, files, workspace)
    linker.resolve_imports()
    assert linker.imports == {"internal": 0, "workspace": 4, "external": 0, "stdlib": 0, "unresolved": 1}
    assert linker.unresolved_imports == ["apps/web/src/page.tsx: @acme/ui/internal (not exported by @acme/ui)"]
    bound = linker.external_bindings["apps/web/src/page.tsx"]
    assert bound["Button"] == ("@acme/ui", "packages/ui/src/button.tsx", "Button")
    assert bound["Order"] == ("@acme/shared", "packages/shared/types", "Order"), "no exports map: any subpath is importable"
    assert "y" not in bound, "a sibling without entry points is a workspace import with nothing to bind"


def test_tsconfig_extends_aliases_reach_into_sibling_packages():
    configs = [{"config": "apps/web/tsconfig.json", "dir": "apps/web", "root": "", "paths": {"@acme/shared": ["packages/shared/src/index.ts"], "@/*": ["apps/web/src/*"]}, "evidence": []}]
    files = [
        _summary("apps/web/package.json", ("package_declaration", {"ecosystem": "npm", "name": "web", "manifest_dir": "apps/web"})),
        _summary("apps/web/src/lib/util.ts", ("symbol", {"name": "fmt", "symbol_kind": "function"})),
        _summary("apps/web/src/page.tsx", *[("import", {"specifier": s, "names": n, "form": "import"}) for s, n in [("@acme/shared", ["Order"]), ("@/lib/util", ["fmt"])]]),
    ]
    workspace = [{"ecosystem": "npm", "name": "@acme/shared", "manifest_dir": "packages/shared", "entries": {".": "packages/shared/src/index.ts"}}]
    linker = _Linker({"name": "apps/web", "paths": [], "ts_configs": configs}, files, workspace)
    linker.resolve_imports()
    assert linker.imports == {"internal": 1, "workspace": 1, "external": 0, "stdlib": 0, "unresolved": 0}, "an alias needs no package.json declaration"
    assert linker.lookup("apps/web/src/page.tsx", "fmt") == ("apps/web/src/lib/util.ts", "fmt")
    assert linker.external_bindings["apps/web/src/page.tsx"]["Order"] == ("@acme/shared", "packages/shared/src/index.ts", "Order")


def test_prisma_call_sites_link_to_prisma_tables_and_tests_link_to_symbols():
    files = [
        _summary("api/package.json", ("package_declaration", {"ecosystem": "npm", "name": "api", "manifest_dir": "api"})),
        _summary("api/prisma/schema.prisma", ("entity", {"kind": "Table", "natural_key": {"datastore": "default", "name": "order_items"}, "name": "order_items", "attributes": {"orm": "prisma", "model": "OrderItem"}})),
        _ts("api/src/db.ts", 'import { PrismaClient } from "@prisma/client";\nexport const prisma = new PrismaClient();\n'),
        _ts("api/src/items.ts", 'import { prisma } from "./db";\nexport async function items() { return prisma.orderItem.findMany(); }\nexport async function ghosts() { return prisma.ghost.findMany(); }\nexport async function raw(q: string) { return prisma.$queryRawUnsafe(q); }\n'),
        _ts("api/src/items.spec.ts", 'import { test } from "@jest/globals";\nimport { items } from "./items";\ntest("items", async () => { await items(); });\n'),
    ]
    linker = _Linker({"name": "api", "paths": []}, files, [])
    linker.resolve_imports()
    assert linker.link_data_access() == {"linked": 1, "unresolved_model": 1, "dynamic_sql": 1}
    rel = [(o["payload"]["subject"]["name"], o["payload"]["predicate"], o["payload"]["object"]["name"]) for o in linker.out if o["kind"] == "relation"]
    assert rel == [("items", "READS_FROM", "order_items")]
    assert sorted(u["reason"] for u in linker.unknowns) == ["dynamic_sql: the statement is built at runtime", "model ghost does not resolve to a table in this unit"]
    linker.link_tests()
    tested = [o for o in linker.out if o["kind"] == "relation" and o["payload"]["predicate"] == "TESTED_BY"]
    assert [(t["payload"]["subject"]["name"], t["payload"]["object"]["name"], t["resolution"]) for t in tested] == [("items", "items", "candidate")]
    test_entity = next(o for o in linker.out if o["kind"] == "entity" and o["payload"]["kind"] == "Test")
    assert test_entity["payload"]["attributes"] == {"framework": "jest"}


def test_f02_api_to_database_chain_and_dynamic_sql_unknown(graphs):
    """Acceptance test 2 on the TypeScript monorepo: route, handler, ORM call and table, with spans."""
    g = graphs("F02-ts-monorepo")[6]
    for route, predicate in [("GET /orders", "READS_FROM"), ("POST /orders", "WRITES_TO"), ("DELETE /orders/:id", "WRITES_TO")]:
        impl = g["edges"][(route, "IMPLEMENTS", route)]
        data = g["edges"][(route, predicate, "orders")]
        assert impl["resolution"] == data["resolution"] == "exact" and len(data["evidence_ids"]) == 2
    assert g["edges"][("listOrders", "READS_FROM", "orders")]["evidence_class"] == "E3"
    assert ("searchOrders", "READS_FROM", "dynamic_sql: the statement is built at runtime") in g["unknowns"]
    assert g["edges"][("listOrders", "TESTED_BY", "orders > lists newest first")]["resolution"] == "candidate"
    imports = g["coverage"][("import_resolution", "apps/web")]["detail"]
    assert imports["workspace_bound"] == ["apps/web/app/page.tsx: @acme/shared -> packages/shared/src/index.ts", "apps/web/app/page.tsx: @acme/ui -> packages/design/src/index.ts"]


def _sql(path, source):
    from repo_intel.extractors.contracts_data import SqlDdl
    out = SqlDdl().extract(path, source.encode(), "cd" * 32)
    return {"path": path, "observations": [o.model_dump(mode="json") for o in out.observations]}


def test_sql_migrations_replay_across_files_in_revision_order():
    """GI-010: the unit's schema is what is left after every migration ran, in the tool's order."""
    files = [
        _sql("db/migration/V1_10__drop_staging.sql", "DROP TABLE staging;\nALTER TABLE orders RENAME COLUMN sku TO product_sku;"),
        _sql("db/migration/V1_2__note.sql", "ALTER TABLE orders ADD COLUMN note text;\nALTER TABLE orders ALTER COLUMN id TYPE bigint;"),
        _sql("db/migration/V1__init.sql", "CREATE TABLE orders (id int, sku text);\nCREATE TABLE staging (id int);"),
    ]
    linker = _Linker({"name": "."}, files, [])
    linker.link_migrations()
    tables = [o["payload"]["natural_key"]["name"] for o in linker.out if o["kind"] == "entity" and o["payload"]["kind"] == "Table"]
    fields = {o["payload"]["natural_key"]["name"]: (o["path"], o["payload"]["attributes"]) for o in linker.out if o["kind"] == "entity" and o["payload"]["kind"] == "Field"}
    assert tables == ["orders"], "staging was created by V1 and dropped by V1.10"
    assert fields == {
        "id": ("db/migration/V1__init.sql", {"type": "BIGINT"}),
        "note": ("db/migration/V1_2__note.sql", {"type": "TEXT"}),
        "product_sku": ("db/migration/V1_10__drop_staging.sql", {"type": "TEXT"}),
    }
    contains = [(o["payload"]["subject"]["natural_key"]["name"], o["payload"]["object"]["natural_key"]["name"]) for o in linker.out if o["kind"] == "relation"]
    assert sorted(contains) == [("orders", "id"), ("orders", "note"), ("orders", "product_sku")]


def test_f02_prisma_migrations_migrate_the_schema(graphs):
    """GI-010 on the monorepo: migrations MIGRATES what they touch; the dropped staging table is gone."""
    before, after = graphs("F02-ts-monorepo")[6], graphs("F02-ts-monorepo")[7]
    assert "orders_staging" not in {e["name"] for e in after["entities"].values()}
    assert after["edges"][("20261101090000_init", "MIGRATES", "orders")]["qualifiers"] == {"op": "create_table"}
    assert after["edges"][("20261120090000_order_note", "MIGRATES", "note")]["qualifiers"] == {"op": "add_column", "column": "note", "type": "TEXT"}
    assert after["edges"][("orders", "CONTAINS", "note")]["evidence_class"] in ("E1", "E2")
    assert ("20261120090000_order_note", "MIGRATES", "orders") in after["edges"] and not any(k[0] == "20261101090000_init" for k in before["edges"])


def test_f06_configuration_and_build_links_between_code_and_deployment(graphs):
    """GI-016 / acceptance test 4: code and deployment meet at a ConfigKey name and an image repository."""
    g = graphs("F06-iac-heavy")[0]
    configured = g["edges"][("status-api", "CONFIGURED_BY", "DATABASE_URL")]
    assert (configured["resolution"], configured["evidence_class"]) == ("exact", "E3")
    assert configured["qualifiers"] == {"basis": "shared_config_key", "supplied_by": ["api", "status-api"]}
    assert len(configured["evidence_ids"]) == 3, "the code read and each deployment unit that supplies the key"
    built = g["edges"][("status-api", "BUILT_BY", "deploy")]
    assert (built["resolution"], built["evidence_class"], built["epistemic_status"]) == ("candidate", "E4", "inferred")
    assert g["edges"][("status-api", "BUILT_BY", "Dockerfile")]["qualifiers"]["pipeline"] == "deploy"
    assert g["edges"][("api", "BUILT_BY", "Dockerfile")]["resolution"] == "exact"  # compose `build: .`
    assert g["coverage"][("build_provenance", "@repo")]["numerator"] == 2  # db runs a public image nobody here builds
    assert g["coverage"][("config_supply", "@repo")]["numerator"] == 1
    names = {e["name"] for e in g["entities"].values()}
    assert "status-db" not in names and not any("postgresql://" in repr(a["qualifiers"]) for a in g["edges"].values())


def test_f01_orm_model_class_is_an_alias_of_its_table(graphs):
    a = graphs("F01-python-api")[0]["edges"][("Customer", "ALIAS_OF", "customers")]
    assert (a["resolution"], a["evidence_class"], a["qualifiers"]) == ("exact", "E2", {"basis": "orm_model", "orm": "sqlalchemy"})


def test_deployment_units_running_one_image_are_candidate_aliases():
    from repo_intel.compile.link import link_repo

    class G:
        def __init__(self):
            self.assertions = []

        def cite(self, ev):
            return ev["id"]

        def assertion(self, a):
            self.assertions.append(a)

    ev = [_ev(i) for i in range(3)]
    du = lambda i, name, attrs: {"id": f"du{i}", "kind": "DeploymentUnit", "name": name, "natural_key": {"name": name}, "attributes": attrs, "evidence_ids": [ev[i]["id"]]}
    shards = {"@root": {"entities": [du(0, "api", {"image": "ghcr.io/acme/api:1"}), du(1, "api-prod", {"images": ["ghcr.io/acme/api:2", "busybox"]}), du(2, "db", {"image": "postgres:17"})], "assertions": [], "evidence": ev}}
    g = G()
    coverage = link_repo(shards, g)
    assert [(a.subject, a.predicate, a.object, a.qualifiers, a.resolution) for a in g.assertions] == [("du1", "ALIAS_OF", "du0", {"basis": "same_image", "image": "ghcr.io/acme/api"}, "candidate")]
    assert [(c["domain"], c["numerator"], c["denominator"]) for c in coverage] == [("build_provenance", 0, 3)]
