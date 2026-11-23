"""Cross-file linking: graded resolution inside a unit, contract matching across units."""

import pytest

from conftest import REPO_ID
from repo_intel.contracts import Evidence, Span, entity_id
from repo_intel.fixtures import load_golden, materialize
from repo_intel.link import _Linker, match_call, normalize_route
from repo_intel.pipeline import compile_commit, load_shards
from repo_intel.store import ObjectStore


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
    assert [len(c["conflicts"]) for c in commits] == [1, 1, 1, 0]
    assert "GET /users" in commits[0]["conflicts"][0]["basis"]


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
    assert found[0].payload == {"dir": "web", "base_url": ".", "paths": {"@/*": ["./src/*"], "~config": ["./config/index.ts"]}}
    assert strip_jsonc('{"a": "// not a comment", /* x */ "b": [1,],}') == '{"a": "// not a comment",  "b": [1]}'
    assert TsConfig().extract("tsconfig.json", b"{ not json", "ab" * 32).parsed is False
    assert not TsConfig().matches("package.json") and TsConfig().matches("apps/web/tsconfig.app.json")

    files = [
        _summary("web/package.json", ("package_declaration", {"ecosystem": "npm", "name": "web", "manifest_dir": "web"})),
        _summary("web/tsconfig.json", ("ts_paths", found[0].payload)),
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
