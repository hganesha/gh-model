"""HTTP calls in the committed bundle (docs/solution-ir-contract.md, section 8.3).

`typescript_source` records the HTTP method of a client call when the source states it; the
linker's `http_calls` export and the compiler's `workspace_exports.unresolved_calls` carry it;
the bundle commits the exports as `exports.json` in every tier, named in manifest.json with its
digest; `rkp verify` and the publish gate's secret scan check it; and a solution links its
members exactly from their committed bundles, with no compile on this machine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from repo_intel.compile.contracts import Manifest
from repo_intel.extractors.typescript_source import TypeScriptSource
from repo_intel.ingest import secrets
from repo_intel.ingest.publish_secrets import export_findings, leaked_secrets
from repo_intel.knowledge import bundle, bundle_check
from repo_intel.storage.hashing import digest, sha256_hex
from repo_intel.storage.store import ObjectStore

TS = TypeScriptSource()
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "PATH": os.environ.get("PATH", ""),
}

# The web front end: every call it makes is served by the orders service, a separate
# repository, except /api/customers, which nothing serves.
WEB = {
    "package.json": '{"name": "web", "version": "1.0.0", "dependencies": {"axios": "1.7.0"}}\n',
    "src/api.ts": """import axios from "axios";

const api = axios.create({ baseURL: "/api" });

export const listOrders = () => api.get("/orders");
export const placeOrder = (order: unknown) => axios.post("/api/orders", order);
export const showOrder = (id: number) => fetch(`/api/orders/${id}?view=full`, { method: "GET" });
export const cancelOrder = (id: number) => axios({ url: `/api/orders/${id}`, method: "delete" });
export const listCustomers = () => fetch("/api/customers");
export const download = (url: string) => axios.get(url);
""",
}
# The orders service. Its own PUT to /api/orders is served by nothing here, so it is exported
# with its method; its plain fetch of /api/orders is served here and is not exported.
ORDERS = {
    "package.json": '{"name": "orders", "version": "1.0.0", "dependencies": {"express": "4.21.0"}}\n',
    "src/server.ts": """import express from "express";

const app = express();
app.get("/api/orders", list);
app.post("/api/orders", create);
app.get("/api/orders/:id", show);
app.delete("/api/orders/:id", remove);

export async function audit() {
  await fetch("/api/orders", { method: "PUT" });
  await fetch("/api/orders");
}

app.listen(3000);
""",
}


def extract(path: str, source: str):
    data = source.encode()
    return TS.extract(path, data, sha256_hex(data)), data


def calls(result) -> list[dict]:
    return [o.payload for o in result.observations if o.kind == "http_call"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false", *args], env=_GIT_ENV, check=True, capture_output=True)


def _repository(path: Path, files: dict[str, str], tier: str = "architecture") -> Path:
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    (path / ".rkp").mkdir(parents=True, exist_ok=True)
    (path / ".rkp" / "config.json").write_text(json.dumps({"bundle_format": bundle.FORMAT, "repository_id": f"local/{path.name}", "commit": tier}))
    _git(path, "init", "-q", "-b", "main")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return path


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    """Both repositories compiled on this machine, once per tier."""
    out = {}
    for tier in bundle.TIERS:
        root = tmp_path_factory.mktemp(tier)
        for name, files in (("web", WEB), ("orders", ORDERS)):
            repo = _repository(root / name, files, tier)
            out[(tier, name)] = (repo, bundle.compile_bundle(repo))
    return out


def _exports_file(repo: Path) -> Path:
    return repo / ".rkp" / "knowledge" / bundle.EXPORTS_FILE


def _copy(repo: Path, dest: Path) -> Path:
    """The repository's committed bundle alone: no source, no cache."""
    shutil.copytree(repo / ".rkp", dest / ".rkp", ignore=shutil.ignore_patterns("cache"))
    return dest


def _as_old_bundle(repo: Path) -> None:
    """Rewrite a bundle as one written before exports.json existed."""
    manifest = repo / ".rkp" / "knowledge" / "manifest.json"
    document = json.loads(manifest.read_text())
    del document["exports"]
    manifest.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    _exports_file(repo).unlink()


def _verify(path: Path) -> tuple[list[str], dict]:
    return bundle_check.verify_bundle(bundle_check.load_bundle(bundle_check.directory_source(path)))


# ------------------------------------------------------------------ the method, per client library


def test_fetch_records_a_literal_method_option_only():
    result, data = extract("src/a.ts", """await fetch("/a", { method: "post" });
await fetch("/b");
await fetch("/c", { headers: {} });
await fetch("/d", options);
await fetch("/e", { method: verb });
await window.fetch("/f", { method: "DELETE" });
""")
    assert calls(result) == [{"path": "/a", "method": "POST"}, {"path": "/b"}, {"path": "/c"}, {"path": "/d"}, {"path": "/e"}, {"path": "/f", "method": "DELETE"}]


def test_axios_verbs_config_calls_and_instances():
    result, data = extract("src/a.ts", """import axios from "axios";
const api = axios.create({ baseURL: "https://user:pw@orders.internal/v1?key=abc" });
const remote = axios.create({ baseURL: process.env.ORDERS_URL });
const plain = axios.create();
axios.get(`/orders/${id}?token=${token}`);
axios.post("/orders", body);
axios.put("/orders/1", body);
axios.patch("/orders/1", body);
axios.delete("/orders/1");
axios.head("/health");
axios({ url: "/cfg", method: "put" });
axios({ url: "/cfg-default" });
axios("/plain", { method: "OPTIONS" });
axios.request({ url: "/req", method: "post" });
api.get("/items");
api.get("https://elsewhere.example/abs");
remote.post("/items");
plain.delete("/x");
axios.get(url);
axios.isCancel(error);
""")
    assert calls(result) == [
        {"path": "/orders/{}", "method": "GET"},
        {"path": "/orders", "method": "POST"},
        {"path": "/orders/1", "method": "PUT"},
        {"path": "/orders/1", "method": "PATCH"},
        {"path": "/orders/1", "method": "DELETE"},
        {"path": "/health", "method": "HEAD"},
        {"path": "/cfg", "method": "PUT"},
        {"path": "/cfg-default"},
        {"path": "/plain", "method": "OPTIONS"},
        {"path": "/req", "method": "POST"},
        {"path": "https://orders.internal/v1/items", "method": "GET"},
        {"path": "https://elsewhere.example/abs", "method": "GET"},
        {"path": "{}/items", "method": "POST"},
        {"path": "/x", "method": "DELETE"},
    ]
    dumped = json.dumps([o.payload for o in result.observations])
    assert "abc" not in dumped and ":pw@" not in dumped and "token=" not in dumped
    assert [d.message for d in result.diagnostics] == ["line 19: axios call URL is not a literal; no http_call recorded"]


def test_axios_by_require_and_namespace_import():
    required, _ = extract("src/a.js", 'const http = require("axios");\nhttp.post("/a");\n')
    namespaced, _ = extract("src/b.ts", 'import * as ax from "axios";\nax.get("/b");\n')
    unrelated, _ = extract("src/c.ts", 'import cache from "./cache";\ncache.get("/c");\n')
    assert calls(required) == [{"path": "/a", "method": "POST"}]
    assert calls(namespaced) == [{"path": "/b", "method": "GET"}]
    assert calls(unrelated) == [], "a get on something that is not an HTTP client is not a call"


def test_angularjs_http_service():
    result, _ = extract("src/app.js", """angular.module("shop").controller("Orders", function ($http) {
  $http.get("/api/orders");
  $http.post("/api/orders", order);
  $http.jsonp("/api/feed");
  $http({ method: "PUT", url: "/api/orders/1" });
  $http({ url: "/api/default" });
  $http({ method: "GET", url: endpoint });
});
""")
    assert calls(result) == [
        {"path": "/api/orders", "method": "GET"}, {"path": "/api/orders", "method": "POST"},
        {"path": "/api/feed", "method": "GET"}, {"path": "/api/orders/1", "method": "PUT"}, {"path": "/api/default"},
    ]
    assert len(result.diagnostics) == 1 and "$http call URL is not a literal" in result.diagnostics[0].message


def test_angular_http_client():
    source = """import { Injectable, inject } from "@angular/core";
import { HttpClient } from "@angular/common/http";

@Injectable({ providedIn: "root" })
export class OrdersService {
  private readonly client = inject(HttpClient);
  constructor(private http: HttpClient) {}
  list() { return this.http.get<Order[]>("/api/orders"); }
  save(order: Order) { return this.http.post("/api/orders", order); }
  remove(id: number) { return this.client.delete(`/api/orders/${id}`); }
  patch() { return this.http.request("PATCH", "/api/orders/1"); }
  feed() { return this.http.jsonp("/api/feed", "callback"); }
}
"""
    result, data = extract("src/app/orders.service.ts", source)
    assert calls(result) == [
        {"path": "/api/orders", "method": "GET"}, {"path": "/api/orders", "method": "POST"},
        {"path": "/api/orders/{}", "method": "DELETE"}, {"path": "/api/orders/1", "method": "PATCH"},
        {"path": "/api/feed", "method": "GET"},
    ]
    first = next(o for o in result.observations if o.kind == "http_call")
    span = first.evidence.span
    assert data[span.start_byte:span.end_byte].decode() == 'this.http.get<Order[]>("/api/orders"'
    # Without the import from @angular/common/http, `this.http` is just a field.
    plain, _ = extract("src/other.ts", source.replace('import { HttpClient } from "@angular/common/http";\n', ""))
    assert calls(plain) == []


# ------------------------------------------------------------------ the export


def test_unresolved_calls_carry_the_method(compiled):
    for tier in bundle.TIERS:
        repo, _ = compiled[(tier, "web")]
        exports = json.loads(_exports_file(repo).read_text())
        assert [(c["path"], c.get("method")) for c in exports["unresolved_calls"]] == [
            ("/api/orders", "GET"), ("/api/orders", "POST"), ("/api/orders/{}", "GET"), ("/api/orders/{}", "DELETE"), ("/api/customers", None),
        ]
        assert all(set(c) <= {"caller", "path", "method", "evidence_ids"} and c["evidence_ids"] for c in exports["unresolved_calls"])
        orders, _ = compiled[(tier, "orders")]
        own = json.loads(_exports_file(orders).read_text())["unresolved_calls"]
        # The PUT is served by nothing in the service; the plain fetch of /api/orders is served there.
        assert [(c["path"], c.get("method")) for c in own] == [("/api/orders", "PUT")]


def test_the_linker_export_carries_the_method():
    from repo_intel.compile.link import _Linker

    result, _ = extract("src/api.ts", WEB["src/api.ts"])
    summary = {"path": "src/api.ts", "observations": [o.row() for o in result.observations]}
    linker = _Linker.__new__(_Linker)
    linker.files = {"src/api.ts": summary}
    linker.has_package = True
    exported = _Linker.exports(linker)["http_calls"]
    assert [(c["call_path"], c.get("method")) for c in exported] == [
        ("/api/orders", "GET"), ("/api/orders", "POST"), ("/api/orders/{}", "GET"), ("/api/orders/{}", "DELETE"), ("/api/customers", None),
    ]
    assert "method" not in exported[-1], "absent, not null, when the source does not state it"


def test_a_stated_method_only_matches_an_endpoint_serving_it():
    from repo_intel.compile.compiler import _http_hits

    endpoint = lambda method: ({"natural_key": {"method": method, "route": "/api/orders"}}, {"id": method}, ".")
    endpoints = [endpoint("GET"), endpoint("POST")]
    assert [e["natural_key"]["method"] for e, _ in _http_hits({"call_path": "/api/orders", "method": "POST"}, endpoints)] == ["POST"]
    assert _http_hits({"call_path": "/api/orders", "method": "PUT"}, endpoints) == []
    assert [e["natural_key"]["method"] for e, _ in _http_hits({"call_path": "/api/orders"}, endpoints)] == ["GET"], "unstated: prefer GET, as before"


# ------------------------------------------------------------------ the bundle


@pytest.mark.parametrize("tier", bundle.TIERS)
def test_exports_round_trip_through_the_bundle(compiled, tier, tmp_path):
    repo, document = compiled[(tier, "web")]
    entry = document["exports"]
    written = json.loads(_exports_file(repo).read_text())
    assert document["tier"] == tier
    assert entry == {"file": "exports.json", "digest": digest(written), "operations": 0, "unresolved_calls": 5}
    store = bundle.store_for(repo)
    assert bundle.compiled_exports(store, Manifest.model_validate(document["snapshot"])) == written

    # Every entity and evidence row the exports cite is in the committed units, in either tier.
    rows = [json.loads(line) for unit in document["units"] for line in (repo / ".rkp" / "knowledge" / unit["file"]).read_text().splitlines()]
    ids = {r["id"] for r in rows}
    assert {c["caller"] for c in written["unresolved_calls"]} <= ids
    assert bundle.export_evidence(written) <= ids

    # A fresh copy without the cache hydrates the exports and indexes them as a compile does.
    clone = _copy(repo, tmp_path / "clone")
    target = ObjectStore(tmp_path / "cache", bundle.TENANT)
    target, manifest = bundle.hydrate(clone, target)
    assert bundle.compiled_exports(target, manifest) == written
    assert bundle.read_exports(clone) == written

    # Compiling again rewrites identical bytes.
    before = _exports_file(repo).read_bytes()
    assert bundle.compile_bundle(repo)["exports"] == entry and _exports_file(repo).read_bytes() == before


def test_an_edited_exports_file_is_refused(compiled, tmp_path):
    repo, _ = compiled[("architecture", "web")]
    clone = _copy(repo, tmp_path / "clone")
    exports = json.loads(_exports_file(clone).read_text())
    exports["unresolved_calls"][0]["path"] = "/api/elsewhere"
    _exports_file(clone).write_text(json.dumps(exports))
    with pytest.raises(bundle.BundleError, match=r"does not match its manifest \(exports\)"):
        bundle.hydrate(clone, ObjectStore(tmp_path / "cache", bundle.TENANT))
    with pytest.raises(bundle.BundleError, match="exports.json does not match"):
        bundle.read_exports(clone)


def test_an_old_bundle_without_exports_still_reads(compiled, tmp_path):
    repo, document = compiled[("architecture", "web")]
    clone = _copy(repo, tmp_path / "clone")
    _as_old_bundle(clone)
    target, manifest = bundle.hydrate(clone, ObjectStore(tmp_path / "cache", bundle.TENANT))
    assert manifest.rkp_hash == document["rkp_hash"]
    assert bundle.compiled_exports(target, manifest) is None and bundle.read_exports(clone) is None
    problems, checked = _verify(clone)
    assert problems == [] and checked["exports"].startswith("absent")


# ------------------------------------------------------------------ rkp verify


@pytest.mark.parametrize("tier", bundle.TIERS)
def test_verify_checks_the_exports(compiled, tier, tmp_path):
    repo, _ = compiled[(tier, "web")]
    problems, checked = _verify(repo)
    assert problems == [] and checked["exports"] == {"operations": 0, "unresolved_calls": 5}

    def damaged(edit) -> list[str]:
        clone = _copy(repo, tmp_path / f"case{len(list(tmp_path.iterdir()))}")
        edit(clone)
        return _verify(clone)[0]

    def rewrite(clone, change):
        exports = json.loads(_exports_file(clone).read_text())
        change(exports)
        _exports_file(clone).write_text(json.dumps(exports))

    assert damaged(lambda c: rewrite(c, lambda e: e["unresolved_calls"][0].update(path="/edited"))) == ["exports.json: does not hash to the digest in manifest.json"]
    assert damaged(lambda c: _exports_file(c).unlink()) == ["exports.json: named by the manifest but missing"]
    assert damaged(lambda c: _exports_file(c).write_text("{not json")) == ["exports.json: not valid JSON"]

    def unnamed(clone):
        manifest = clone / ".rkp" / "knowledge" / "manifest.json"
        document = json.loads(manifest.read_text())
        del document["exports"]
        manifest.write_text(json.dumps(document))

    assert damaged(unnamed) == ["exports.json: not named by the manifest"]

    def dangling(clone):
        rewrite(clone, lambda e: e["unresolved_calls"][1].update(caller="ent_aaaaaaaaaaaaaaaaaaaaaaaaaa", evidence_ids=["ev_aaaaaaaaaaaaaaaaaaaaaaaaaa"], method="post"))
        manifest = clone / ".rkp" / "knowledge" / "manifest.json"
        document = json.loads(manifest.read_text())
        document["exports"]["digest"] = digest(json.loads(_exports_file(clone).read_text()))
        manifest.write_text(json.dumps(document))

    assert damaged(dangling) == [
        "exports.json: unresolved_calls[1]: caller ent_aaaaaaaaaaaaaaaaaaaaaaaaaa is not an entity of the bundle",
        "exports.json: unresolved_calls[1]: cites evidence the bundle does not hold (ev_aaaaaaaaaaaaaaaaaaaaaaaaaa)",
        "exports.json: unresolved_calls[1]: method 'post' is not an upper-case HTTP method",
    ]

    def leaking(clone):
        rewrite(clone, lambda e: e["unresolved_calls"][4].update(path="/api/customers?api_key=abc123"))
        manifest = clone / ".rkp" / "knowledge" / "manifest.json"
        document = json.loads(manifest.read_text())
        document["exports"]["digest"] = digest(json.loads(_exports_file(clone).read_text()))
        manifest.write_text(json.dumps(document))

    assert damaged(leaking) == ["publish gate: exports.json: unresolved_calls[4].path: a query or fragment value"]


def test_verify_reads_the_exports_from_a_commit(compiled):
    repo, _ = compiled[("architecture", "orders")]
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "knowledge")
    loaded = bundle_check.load_bundle(bundle_check.GitSource(repo, "HEAD"))
    problems, checked = bundle_check.verify_bundle(loaded)
    assert problems == [] and checked["exports"] == {"operations": 0, "unresolved_calls": 1}


def test_rkp_verify_reports_an_exports_problem(compiled, tmp_path, capsys):
    from repo_intel import cli

    repo, _ = compiled[("full", "web")]
    clone = _copy(repo, tmp_path / "clone")
    assert cli.main(["verify", str(clone), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["checked"]["exports"] == {"operations": 0, "unresolved_calls": 5}
    _exports_file(clone).write_text(_exports_file(clone).read_text().replace("/api/customers", "/api/clients"))
    assert cli.main(["verify", str(clone), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["problems"] == ["exports.json: does not hash to the digest in manifest.json"]


# ------------------------------------------------------------------ the secret scan


def test_publish_gate_scans_the_exports(store):
    source = b"DATABASE_URL=postgres://app:POST@db/work\nAWS_KEY=AKIAIOSFODNN7EXAMPLE\n"
    entry = {"path": "config.env", "blob_sha256": store.put_bytes(source).split(":", 1)[1], "secret_findings": secrets.scan(source)}
    assert [f["detector"] for f in entry["secret_findings"]] == ["url_credentials", "aws_access_key_id"]
    manifest = {"entries": [entry]}
    call = {"caller": "ent_aaaaaaaaaaaaaaaaaaaaaaaaaa", "path": "/api/orders", "method": "POST", "evidence_ids": ["ev_aaaaaaaaaaaaaaaaaaaaaaaaaa"]}
    # A method is a fixed protocol word, never a leak of a password that happens to equal it.
    assert leaked_secrets(store, manifest, [{"operations": [], "unresolved_calls": [call]}]) == []
    assert leaked_secrets(store, manifest, [{"operations": [], "unresolved_calls": [{**call, "path": "/hooks/AKIAIOSFODNN7EXAMPLE"}]}]) == ["config.env"]
    assert leaked_secrets(store, manifest, [{"operations": [], "unresolved_calls": [{**call, "path": "/db/POST/x"}]}]) == ["config.env"], "the same word outside the method field still counts"


def test_export_findings_need_no_source():
    call = {"caller": "ent_a", "path": "/api/orders", "method": "GET", "evidence_ids": ["ev_a"]}
    operation = {"interface": "ent_i", "component": None, "method": "GET", "route": "/orders.cfc?method=list", "evidence_ids": ["ev_a"]}
    assert export_findings({"operations": [operation], "unresolved_calls": [call, {**call, "path": "/a?token="}, {**call, "path": "{}/a/{}"}]}) == []
    found = export_findings({"operations": [{**operation, "route": "/orders#top"}], "unresolved_calls": [
        {**call, "path": "/hooks/AKIAIOSFODNN7EXAMPLE"},
        {**call, "path": "https://app:hunter2pw@db.example/x"},
        {**call, "path": "/a?token=abc123"},
    ]})
    assert found == [
        "operations[0].route: a query or fragment value",
        "unresolved_calls[0].path: aws_access_key_id",
        "unresolved_calls[1].path: url_credentials",
        "unresolved_calls[2].path: a query or fragment value",
    ]
    assert not any("AKIA" in f or "hunter2" in f or "abc123" in f for f in found), "a finding names where, never the value"


def test_a_planted_secret_in_a_call_url_never_reaches_the_exports(tmp_path):
    repo = _repository(tmp_path / "web", {**WEB, "src/hooks.ts": 'export const ping = () => fetch("/hooks/AKIAIOSFODNN7EXAMPLE/ping", { method: "POST" });\n'})
    bundle.compile_bundle(repo)
    text = (repo / ".rkp" / "knowledge").joinpath(bundle.EXPORTS_FILE).read_text()
    assert "AKIAIOSFODNN7EXAMPLE" not in text
    assert _verify(repo)[0] == []


# ------------------------------------------------------------------ a solution from committed bundles only


def _solution(tmp_path: Path, compiled, tier: str = "architecture") -> Path:
    """Two members delivered as bundles alone, with no source and no cache."""
    root = tmp_path / "solution"
    (root / ".rkp").mkdir(parents=True)
    for name in ("web", "orders"):
        _copy(compiled[(tier, name)][0], tmp_path / "delivered" / name)
    (root / ".rkp" / "solution.json").write_text(json.dumps({
        "schema": "rkp-solution/1", "name": "shop",
        "members": [{"id": "web", "bundle": "../delivered/web/.rkp", "role": "web"}, {"id": "orders", "bundle": "../delivered/orders/.rkp", "role": "service"}],
    }))
    return root


def test_a_solution_links_members_exactly_from_committed_bundles(compiled, tmp_path):
    from repo_intel.solution import commands, load_solution

    root = _solution(tmp_path, compiled)
    solution = load_solution(root)
    assert solution.problems == [] and solution.warnings == []
    for member in ("web", "orders"):
        data = solution.view.members[member]
        assert data.exports is not None and data.info["tier"] == "architecture" and data.info["detail"] == "architecture"
        assert not (tmp_path / "delivered" / member / ".rkp" / "cache").exists(), "nothing written into a member"
    assert (root / ".rkp" / "cache" / "solution" / "web").is_dir()

    r = commands.links(root)
    http = sorted((x["rule"], x["from_member"], x["to_member"], x["to_name"], x["evidence_class"]) for x in r["links"] if x["rule"].startswith("endpoint"))
    assert http == [
        ("endpoint_method_path", "web", "orders", "DELETE /api/orders/:id", "E3"),
        ("endpoint_method_path", "web", "orders", "GET /api/orders", "E3"),
        ("endpoint_method_path", "web", "orders", "GET /api/orders/:id", "E3"),
        ("endpoint_method_path", "web", "orders", "POST /api/orders", "E3"),
    ]
    assert [x for x in r["candidates"] if x["rule"].startswith("endpoint")] == [], "every stated method picks one endpoint"
    unmatched = sorted((u["member"], u["reference"], u.get("method"), u["reason"]) for u in r["unmatched"])
    assert unmatched == [("orders", "/api/orders", "PUT", "no_endpoint"), ("web", "/api/customers", None, "no_endpoint")]
    coverage = {c["scope"]: (c["numerator"], c["denominator"], c["detail"]["available"]) for c in r["coverage"] if c["domain"] == "solution_http_calls"}
    assert coverage == {"web": (4, 5, True), "orders": (0, 1, True)}


def test_a_solution_of_old_bundles_keeps_the_warning(compiled, tmp_path):
    from repo_intel.solution import load_solution

    root = _solution(tmp_path, compiled, "full")
    _as_old_bundle(tmp_path / "delivered" / "web")
    solution = load_solution(root)
    assert solution.problems == []
    assert solution.view.members["web"].exports is None and solution.view.members["orders"].exports is not None
    assert [w for w in solution.warnings if "HTTP calls are not in the committed bundle" in w] == [
        "member web: its HTTP calls are not in the committed bundle; compile it on this machine (`rkp compile ../delivered/web/.rkp`) to link them"
    ]
