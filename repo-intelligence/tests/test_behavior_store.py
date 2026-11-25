"""The behaviour store: `behavior` observations compiled into shards, kept in the bundle's detail
tier, answered by the local API and summarised (repo_intel.knowledge.behavior_store)."""

import io
import json
import re
import shutil

import pytest

from repo_intel import extractors, platforms
from repo_intel.compile import behavior, buildgraph, compiler
from repo_intel.compile.contracts import Manifest, entity_id
from repo_intel.compile.fixtures import _git
from repo_intel.compile.ontology import EntityKind
from repo_intel.compile.pipeline import load_shards
from repo_intel.extractors.base import Extraction, entity, find_span, obs, ref, whole_file
from repo_intel.knowledge import behavior_store, bundle, local_api
from repo_intel.platforms import Construct, PlatformPack, Term

K = EntityKind
REPO_ID = "local/repo"


class DemoSource:
    """Reads a `.demo` file: a JSON document naming a program, its page body and its functions.

    Each function becomes a Symbol with a behaviour unit; `"declare": false` emits the unit
    without the Symbol, and `"raw": true` emits an observation that skips validation.
    """

    id, version = "demo.source", "1.0.0"

    def matches(self, path):
        return path.endswith(".demo")

    def extract(self, path, data, blob):
        doc = json.loads(data)
        at = whole_file(data)
        program = ref(K.PROGRAM, {"platform": "demo", "name": doc["program"]}, doc["program"])
        out = [platforms.platform_entity(blob, at, "demo:Template", program["natural_key"], program["name"])]
        out.append(behavior.behavior_observation(blob, find_span(data, '"page"'), program, "demo", doc["page"], kind="page"))
        for f in doc["functions"]:
            owner = ref(K.SYMBOL, {"platform": "demo", "program": doc["program"], "name": f["name"]}, f["name"])
            where = find_span(data, f'"{f["name"]}"')
            if f.get("raw"):
                out.append(obs("behavior", blob, where, {"owner": f.get("owner", owner), "language": "demo", "params": [], "body": f["body"]}))
                continue
            if f.get("declare", True):
                out.append(entity(blob, where, K.SYMBOL, owner["natural_key"], owner["name"]))
            out.append(behavior.behavior_observation(blob, where, owner, "demo", f["body"], f.get("params"), kind=f.get("kind")))
        for construct_id, modelled in doc.get("constructs", []):
            out.append(platforms.construct(blob, at, construct_id, modelled))
        return Extraction(observations=out, parsed=True)


DEMO = PlatformPack(
    id="demo", name="Demo platform", prefix="demo", version="1.0.0",
    terms=(Term("demo:Template", K.PROGRAM, "lossy", "a page template with code"),),
    constructs=(
        Construct("demo:lock", "a named lock", "behavior:lock"),
        Construct("demo:magic", "a construct with no neutral form", "unmapped"),
    ),
    extractors=(DemoSource(),),
)

LIT = lambda v: {"e": "lit", "value": v, "type": "string"}  # noqa: E731
VAR = lambda name, scope="variables": {"e": "var", "scope": scope, "name": name}  # noqa: E731

PAGE = [
    {"op": "include", "at": [1, 2], "target": LIT("header.demo")},
    {"op": "include", "at": [2, 3], "target": VAR("skin", "url")},
    {"op": "query", "at": [3, 4], "sql": "SELECT o.id, c.name FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.id = :id",
     "params": [{"name": "id", "value": VAR("id", "url"), "type": "integer"}], "datasource": "shopdsn", "target": VAR("order")},
    {"op": "call", "at": [4, 5], "call": {"e": "call", "callee": {"e": "name", "name": "writeLog", "kind": "builtin"}, "args": [{"e": "template", "parts": ["viewed ", VAR("id", "url")]}]}},
    {"op": "call", "at": [5, 6], "call": {"e": "call", "callee": {"e": "index", "object": VAR("handlers"), "index": VAR("action", "form")}}},
    {"op": "assign", "at": [6, 7], "target": VAR("svc"), "value": {"e": "new", "type": "OrderService"}},
    {"op": "if", "at": [7, 9], "cond": {"e": "unop", "op": "not", "operand": VAR("order")},
     "then": [{"op": "redirect", "at": [8, 9], "target": LIT("missing.demo")}]},
    {"op": "output", "at": [9, 10], "channel": "response", "value": {"e": "member", "object": VAR("order"), "name": "id"}},
    {"op": "output", "at": [10, 11], "channel": "log", "value": LIT("done")},
]
SAVE = [
    {"op": "lock", "at": [1, 9], "name": "orders", "body": [
        {"op": "transaction", "at": [2, 8], "body": [
            {"op": "query", "at": [3, 4], "sql": "UPDATE orders SET qty = :qty WHERE id = :id", "datasource": "shopdsn",
             "params": [{"name": "qty", "value": VAR("qty", "form"), "type": "integer"}, {"name": "id", "value": VAR("id", "arguments"), "type": "integer"}]},
            {"op": "query", "at": [4, 5], "sql": "INSERT INTO audit_log (who, what) VALUES (:who, 'save')", "datasource": "shopdsn"},
        ]},
    ]},
    {"op": "call", "at": [9, 10], "call": {"e": "call", "callee": {"e": "member", "object": VAR("svc"), "name": "notify"}, "args": []}},
    {"op": "call", "at": [10, 11], "call": {"e": "call", "callee": {"e": "name", "name": "writeLog", "kind": "builtin"}, "args": [LIT("saved")]}},
    {"op": "platform", "at": [11, 12], "construct": "demo:magic", "detail": {"mode": "fast"}},
    {"op": "redirect", "at": [12, 13], "target": {"e": "template", "parts": ["orders.demo?id=", VAR("id", "arguments")]}},
]
SOURCE = {
    "program": "orders",
    "page": PAGE,
    "functions": [
        {"name": "save", "kind": "function", "params": [{"name": "id", "type": "integer", "required": True}], "body": SAVE},
        {"name": "lookup", "body": [{"op": "return", "at": [1, 2], "value": LIT("x")}]},
        {"name": "ghost", "declare": False, "body": [{"op": "exit", "at": [1, 2]}]},
        {"name": "broken", "raw": True, "body": "not a list"},
    ],
    "constructs": [["demo:lock", True], ["demo:magic", False]],
}
PLAIN = {"program": "about", "page": [{"op": "output", "at": [1, 2], "channel": "response", "value": LIT("hello")}], "functions": []}


@pytest.fixture
def demo(monkeypatch):
    registry = platforms._load()
    monkeypatch.setitem(registry, "PACKS", {"demo": DEMO})
    monkeypatch.setitem(registry, "TERMS", {t.name: (DEMO, t) for t in DEMO.terms})
    monkeypatch.setitem(registry, "CONSTRUCTS", {c.id: (DEMO, c) for c in DEMO.constructs})
    # As repo_intel.extractors would merge a discovered pack: its extractor runs with the others.
    monkeypatch.setattr(extractors, "EXTRACTORS", sorted([*extractors.EXTRACTORS, *DEMO.extractors], key=lambda e: e.id))
    # The pack exists only in this process, so steps must run here, not in a sandboxed worker.
    monkeypatch.setenv(buildgraph.EXECUTOR_ENV, "inprocess")
    yield DEMO
    extractors._EXTRACTOR_VERSIONS.clear()


def _make_repo(path, tier=None):
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "app").mkdir()
    (path / "app" / "orders.demo").write_text(json.dumps(SOURCE, indent=1, sort_keys=True))
    (path / "app" / "about.demo").write_text(json.dumps(PLAIN, indent=1, sort_keys=True))
    if tier:
        bundle.ensure_layout(path)
        config = path / ".rkp" / "config.json"
        config.write_text(json.dumps({**json.loads(config.read_text()), "commit": tier}))
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "demo", date="2026-02-01T00:00:00+00:00")
    return path


@pytest.fixture
def full_repo(demo, tmp_path):
    return _make_repo(tmp_path / "repo", "full")


@pytest.fixture
def arch_repo(demo, tmp_path):
    return _make_repo(tmp_path / "repo")


def _ids():
    program = entity_id(REPO_ID, K.PROGRAM, {"platform": "demo", "name": "orders"})
    symbol = lambda name: entity_id(REPO_ID, K.SYMBOL, {"platform": "demo", "program": "orders", "name": name})  # noqa: E731
    return program, symbol


def _unit_rows(repo, document):
    knowledge = repo / ".rkp" / "knowledge"
    return [json.loads(line) for unit in document["units"] for line in (knowledge / unit["file"]).read_text().splitlines()]


def _files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# ------------------------------------------------------------------ compile


def test_behaviour_is_compiled_into_shards_by_owner(full_repo):
    document = bundle.compile_bundle(full_repo)
    shards = load_shards(bundle.store_for(full_repo), Manifest.model_validate(document["snapshot"]))
    program, symbol = _ids()
    rows = [b for s in shards.values() for b in s.get("behaviors", [])]
    by_owner = {}
    for b in rows:
        by_owner.setdefault(b["owner"], []).append(b)
    about = entity_id(REPO_ID, K.PROGRAM, {"platform": "demo", "name": "about"})
    assert set(by_owner) == {program, about, symbol("save"), symbol("lookup")}
    [save] = by_owner[symbol("save")]
    assert save["body"] == SAVE and save["language"] == "demo" and save["schema"] == behavior.SCHEMA
    assert save["params"] == [{"name": "id", "type": "integer", "required": True}]
    assert save["evidence_class"] == "E2" and save["id"].startswith("bhv_")
    assert save["kind"] == "function" and by_owner[program][0]["kind"] == "page"
    assert "kind" not in by_owner[symbol("lookup")][0], "a unit without a kind has none in its row"

    [shard] = [s for s in shards.values() if s.get("behaviors")]
    assert [b["id"] for b in shard["behaviors"]] == sorted(b["id"] for b in shard["behaviors"])
    evidence = {e["id"]: e for e in shard["evidence"]}
    entities = {e["id"]: e for e in shard["entities"]}
    for b in shard["behaviors"]:
        [ev] = b["evidence_ids"]
        assert evidence[ev]["observation_kind"] == "behavior"
        assert entities[b["artifact"]]["kind"] == "Artifact"
        assert entities[b["owner"]]["kind"] in ("Program", "Symbol")
    # Behaviour stays detail: no relations are derived from it in this wave.
    assert not [a for a in shard["assertions"] if a["predicate"] == "CALLS"]


def test_an_owner_that_does_not_resolve_is_an_unknown_not_a_crash(full_repo):
    document = bundle.compile_bundle(full_repo)
    store = bundle.store_for(full_repo)
    manifest = Manifest.model_validate(document["snapshot"])
    assert manifest.status == "complete"  # the publish gate passed
    shards = load_shards(store, manifest)
    [shard] = [s for s in shards.values() if s.get("behaviors")]
    _, symbol = _ids()
    assert symbol("ghost") not in {b["owner"] for b in shard["behaviors"]}
    assert symbol("ghost") not in {e["id"] for e in shard["entities"]}, "an unresolved owner is not declared by its behaviour"
    unknowns = [u for u in shard["unknowns"] if u["predicate"] == compiler.BEHAVIOR_OWNER]
    reasons = [u["reason"] for u in unknowns]
    assert len(reasons) == 2
    assert any("Symbol 'ghost', which nothing in this unit declares" in r for r in reasons), reasons
    assert any("is not valid: body: a block must be a list" in r for r in reasons), reasons
    artifact = entity_id(REPO_ID, K.ARTIFACT, {"path": "app/orders.demo"})
    assert {u["subject"] for u in unknowns} == {artifact}
    assert {u["coverage_ref"] for u in unknowns} == {f"behavior:{shard['unit']}"}

    coverage = [c for c in store.get_json(manifest.coverage_ref) if c["domain"] == "behavior"]
    assert coverage == [{"domain": "behavior", "scope": shard["unit"], "numerator": 4, "denominator": 6, "detail": {"by_language": {"demo": 4}}}]
    constructs = [c for c in store.get_json(manifest.coverage_ref) if c["domain"] == "platform_constructs"]
    assert constructs[0]["scope"] == "demo" and (constructs[0]["numerator"], constructs[0]["denominator"]) == (1, 2)


def test_owner_resolution_in_isolation():
    """Owner refs resolve as relation endpoints do, `@unit` included; a bad kind is an unknown."""
    shard = compiler._Shard("svc")
    blob = "b" * 64
    at = whole_file(b"0123456789")
    artifact = shard.entity(compiler.Entity.make("r", K.ARTIFACT, {"path": "svc/a.demo"}, "a.demo"))
    owner = shard.entity(compiler.Entity.make("r", K.SYMBOL, {"path": "svc", "name": "f"}, "f"))
    good = behavior.behavior_observation(blob, at, {"kind": "Symbol", "natural_key": {"path": "@unit", "name": "f"}, "name": "f"}, "demo", [{"op": "exit"}]).model_dump(mode="json")
    compiler._behavior(shard, "r", "svc", artifact, good)
    bad_kind = {**good, "payload": {**good["payload"], "owner": {"kind": "Nope", "natural_key": {}, "name": "x"}}}
    compiler._behavior(shard, "r", "svc", artifact, bad_kind)
    rows = shard.rows()
    assert [b["owner"] for b in rows["behaviors"]] == [owner]
    assert [u["predicate"] for u in rows["unknowns"]] == [compiler.BEHAVIOR_OWNER]
    assert "Nope 'x'" in rows["unknowns"][0]["reason"]
    # A shard with no behaviour has no behaviour table, so its digest is what it was before.
    assert "behaviors" not in compiler._Shard("x").rows()


def test_compiling_again_is_deterministic(full_repo, tmp_path):
    first = bundle.compile_bundle(full_repo)
    written = _files(full_repo / ".rkp" / "knowledge")
    shutil.rmtree(full_repo / ".rkp" / "cache")
    again = bundle.compile_bundle(full_repo)
    assert again["rkp_hash"] == first["rkp_hash"]
    assert _files(full_repo / ".rkp" / "knowledge") == written

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(full_repo), str(clone))
    assert bundle.compile_bundle(clone)["rkp_hash"] == first["rkp_hash"]


# ------------------------------------------------------------------ bundle tiers


def test_the_full_tier_keeps_behaviour_and_hydrates_it_exactly(full_repo):
    document = bundle.compile_bundle(full_repo)
    assert document["tier"] == "full" and document["counts"]["behaviors"] == 4
    rows = _unit_rows(full_repo, document)
    behaviours = [r for r in rows if r["table"] == "behavior"]
    assert len(behaviours) == 4 and sum(u["behaviors"] for u in document["units"]) == 4
    assert rows[-len(behaviours):] == behaviours, "behaviour rows follow the other tables"

    compiled = load_shards(bundle.store_for(full_repo), Manifest.model_validate(document["snapshot"]))
    shutil.rmtree(full_repo / ".rkp" / "cache")
    store, manifest = bundle.hydrate(full_repo)
    assert manifest.rkp_hash == document["rkp_hash"]
    assert load_shards(store, manifest) == compiled  # every digest re-verified, behaviour included
    assert bundle.bundle_status(full_repo)["detail"] == "complete"


def test_the_architecture_tier_leaves_behaviour_out(arch_repo):
    document = bundle.compile_bundle(arch_repo)
    assert document["tier"] == "architecture"
    assert document["detail_tables"] == ["behaviors"] and document["detail_kinds"] == ["Symbol"]
    assert document["detail_omitted"]["behaviors"] == 4 and document["counts"]["behaviors"] == 0
    rows = _unit_rows(arch_repo, document)
    assert not [r for r in rows if r["table"] == "behavior"]
    assert not [r for r in rows if r["table"] == "evidence" and r["observation_kind"] == "behavior"], "evidence only behaviour cites is detail too"
    program, _ = _ids()
    assert program in {r["id"] for r in rows if r["table"] == "entity"}, "the Program stays; only its behaviour is left out"

    shutil.rmtree(arch_repo / ".rkp" / "cache")  # a fresh clone
    store, manifest = bundle.hydrate(arch_repo)
    assert not [s for s in load_shards(store, manifest).values() if "behaviors" in s]


def test_a_row_of_an_unknown_table_is_refused(full_repo):
    document = bundle.compile_bundle(full_repo)
    unit = full_repo / ".rkp" / "knowledge" / document["units"][0]["file"]
    unit.write_text(unit.read_text() + json.dumps({"table": "mystery", "id": "x"}) + "\n")
    shutil.rmtree(full_repo / ".rkp" / "cache")
    with pytest.raises(bundle.BundleError, match="table 'mystery'"):
        bundle.hydrate(full_repo)


# ------------------------------------------------------------------ local API


def test_the_local_api_answers_behaviour_with_its_summary(full_repo):
    document = bundle.compile_bundle(full_repo)
    h = document["rkp_hash"]
    program, symbol = _ids()
    api = local_api.LocalApi(full_repo)

    status, body = api.handle("GET", f"snapshots/{h}/behavior/{program}")
    assert status == 200
    assert body["entity"] == {"id": program, "kind": "Program", "name": "orders"}
    assert [u["body"] for u in body["units"]] == [PAGE]
    assert [u["kind"] for u in body["units"]] == ["page"], "the unit's kind is returned with it"
    assert body["summary"]["unit_kinds"] == {"page": 1}
    assert {q["unit_kind"] for q in body["summary"]["queries"]} == {"page"}
    assert body["summary"]["tables"] == {"reads": ["customers", "orders"], "writes": [], "dynamic": []}

    status, body = api.handle("GET", f"snapshots/{h}/behavior/{symbol('save')}")
    assert status == 200 and body["summary"]["tables"]["writes"] == ["audit_log", "orders"]
    assert body["units"][0]["kind"] == "function" and body["summary"]["unit_kinds"] == {"function": 1}

    status, body = api.handle("GET", f"snapshots/{h}/behavior/{entity_id(REPO_ID, K.ARTIFACT, {'path': 'app/orders.demo'})}")
    assert status == 404 and body["code"] == "no_behavior"
    status, body = api.handle("GET", f"snapshots/{h}/behavior/ent_doesnotexist")
    assert status == 404 and body["code"] is None
    status, body = api.handle("GET", f"snapshots/sha256:other/behavior/{program}")
    assert status == 404 and body["code"] == "replaced"

    # The same over the stdio protocol the editor uses.
    stdin = io.StringIO(json.dumps({"id": 7, "method": "GET", "path": f"snapshots/{h}/behavior/{symbol('lookup')}"}) + "\n")
    stdout = io.StringIO()
    local_api.serve_stdio(full_repo, stdin=stdin, stdout=stdout)
    reply = json.loads(stdout.getvalue())
    assert reply["id"] == 7 and reply["status"] == 200 and reply["body"]["summary"]["statements"] == 1
    assert "kind" not in reply["body"]["units"][0] and reply["body"]["summary"]["unit_kinds"] == {}


def test_an_architecture_clone_says_the_detail_is_not_committed(arch_repo):
    document = bundle.compile_bundle(arch_repo)
    program, _ = _ids()
    path = f"snapshots/{document['rkp_hash']}/behavior/{program}"
    status, body = local_api.LocalApi(arch_repo).handle("GET", path)
    assert status == 200, "compiled here: the cache holds the full snapshot"
    assert body["units"]

    shutil.rmtree(arch_repo / ".rkp" / "cache")  # a fresh clone of the committed tier
    status, body = local_api.LocalApi(arch_repo).handle("GET", path)
    assert status == 404 and body["code"] == "detail_not_committed"
    assert "architecture tier" in body["message"]


def test_the_store_loads_behaviour_from_a_snapshot(full_repo):
    document = bundle.compile_bundle(full_repo)
    store, manifest = bundle.store_for(full_repo), Manifest.model_validate(document["snapshot"])
    program, symbol = _ids()
    assert [u["owner"] for u in behavior_store.load(store, manifest, program)] == [program]
    assert behavior_store.load(store, manifest, symbol("ghost")) == []
    assert behavior_store.behavior_of(store, manifest, symbol("ghost")) is None
    found = behavior_store.behavior_of(store, manifest, symbol("save"))
    assert found["entity_id"] == symbol("save") and found["summary"]["units"] == 1
    index = behavior_store.index(load_shards(store, manifest).values())
    assert list(index) == sorted(index) and len(index) == 4


# ------------------------------------------------------------------ the derived summary


def test_summary_of_calls_queries_includes_redirects_and_outputs():
    units = [{"id": "bhv_a", "language": "demo", "body": PAGE}, {"id": "bhv_b", "language": "demo", "body": SAVE}]
    s = behavior_store.summarize(units)
    assert s["units"] == 2 and s["languages"] == ["demo"]
    assert s["calls"] == [
        {"callee": "OrderService", "kind": "new", "count": 1},
        {"callee": "svc.notify", "kind": "method", "count": 1},
        {"callee": "writeLog", "kind": "builtin", "count": 2},
    ]
    assert s["dynamic_calls"] == 1  # handlers[form.action](): the target is computed
    assert [(q["behavior_id"], q["statement"], q["reads"], q["writes"]) for q in s["queries"]] == [
        ("bhv_a", "select", ["customers", "orders"], []),
        ("bhv_b", "update", [], ["orders"]),
        ("bhv_b", "insert", [], ["audit_log"]),
    ]
    assert s["queries"][0]["datasource"] == "shopdsn" and s["queries"][0]["params"] == ["id"] and s["queries"][0]["result"] == "order"
    assert s["queries"][0]["at"] == [3, 4]
    assert s["datasources"] == ["shopdsn"]
    assert s["tables"] == {"reads": ["customers", "orders"], "writes": ["audit_log", "orders"], "dynamic": []}
    assert s["includes"] == [
        {"behavior_id": "bhv_a", "at": [1, 2], "target": "header.demo"},
        {"behavior_id": "bhv_a", "at": [2, 3], "dynamic": True, "expression": "var"},
    ]
    assert s["redirects"] == [
        {"behavior_id": "bhv_a", "at": [8, 9], "target": "missing.demo"},
        {"behavior_id": "bhv_b", "at": [12, 13], "dynamic": True, "expression": "template"},
    ]
    assert s["outputs"] == {"log": 1, "response": 1}
    assert s["platform_constructs"] == {"demo:magic": 1}
    assert s["statements"] == len([n for u in units for n in behavior.walk(u["body"]) if "op" in n])
    assert behavior_store.summarize([])["units"] == 0


@pytest.mark.parametrize("sql, expected", [
    ("SELECT * FROM orders o JOIN customers c ON c.id = o.cid", ("select", ["customers", "orders"], [], [])),
    ("UPDATE orders SET qty = :qty WHERE id = :id", ("update", [], ["orders"], [])),
    ("INSERT INTO audit_log (who) VALUES (:who)", ("insert", [], ["audit_log"], [])),
    ("DELETE FROM sessions WHERE expires < :now", ("delete", [], ["sessions"], [])),
    ("select a.x from sales.a a, sales.b as b, c where x in (select y from d)", ("select", ["c", "d", "sales.a", "sales.b"], [], [])),
    ("WITH recent AS (SELECT * FROM orders), t2 (a) AS (SELECT 1 FROM dual) SELECT * FROM recent JOIN t2 ON 1 = 1 JOIN items USING (id)", ("select", ["items", "orders"], [], [])),
    ('SELECT EXTRACT(YEAR FROM created) FROM "Order Lines" WHERE a IS DISTINCT FROM b', ("select", ["Order Lines"], [], [])),
    ("SELECT * FROM generate_series(1, 3) g, [dbo].[Users] -- FROM comments\n", ("select", ["dbo.Users"], [], [])),
    ("SELECT 'FROM strings' FROM real_t", ("select", ["real_t"], [], [])),
    ("MERGE INTO stock s USING incoming i ON s.id = i.id WHEN MATCHED THEN UPDATE SET qty = i.qty", ("merge", ["incoming"], ["stock"], [])),
    ("INSERT INTO t1 SELECT * FROM t2 ON DUPLICATE KEY UPDATE a = 1", ("insert", ["t2"], ["t1"], [])),
    ("SELECT id INTO :v FROM t FOR UPDATE", ("select", ["t"], [], [])),
    ("select * from mydb..customers", ("select", ["mydb..customers"], [], [])),
    ("SELECT * FROM #tbl# WHERE x = 1", ("select", [], [], ["#tbl#"])),
    ("DATA x; SET &lib..orders", ("data", [], [], [])),
    ("SELECT * FROM &lib..orders", ("select", [], [], ["&lib..orders"])),
    ("", ("", [], [], [])),
])
def test_sql_table_scanner(sql, expected):
    found = behavior_store.sql_tables(sql)
    assert (found["statement"], found["reads"], found["writes"], found["dynamic"]) == expected


# ------------------------------------------------------------------ substitutions in SQL

INLINE = lambda name, scope="url": {"name": name, "value": VAR(name, scope), "type": "any", "inline": True}  # noqa: E731
BIND = lambda name, scope="url": {"name": name, "value": VAR(name, scope), "type": "integer"}  # noqa: E731
MACRO = lambda name: {"name": name, "value": VAR(name, "macro"), "type": "string"}  # noqa: E731


@pytest.mark.parametrize("sql, params, expected", [
    # CFML: FROM orders_#url.region# o JOIN #tbl# t ... WHERE o.id = <cfqueryparam ...>
    ("SELECT o.id FROM orders_:region o JOIN :tbl t ON t.id = o.id WHERE o.id = :id", [INLINE("region"), INLINE("tbl", "variables"), BIND("id")],
     ("select", [], [], ["#tbl#", "orders_#region#"])),
    # CFML queryExecute("... FROM archive_#form.year#_orders ...", {x: 1}): the placeholder runs into the word.
    ("SELECT * FROM archive_:year_orders WHERE x = :x", [INLINE("year", "form"), BIND("x")], ("select", [], [], ["archive_#year#_orders"])),
    ("UPDATE :tbl SET a = :a", [INLINE("tbl", "variables"), BIND("a")], ("update", [], [], ["#tbl#"])),
    ("INSERT INTO log_:suffix (a) SELECT a FROM src", [INLINE("suffix")], ("insert", ["src"], [], ["log_#suffix#"])),
    # SAS (F14): create table ... from stage.customers_&month, with or without the inline flag.
    ("create table rpt.customer_summary as select region from stage.customers_:month group by region", [MACRO("month")],
     ("create", [], ["rpt.customer_summary"], ["stage.customers_#month#"])),
    ("create table rpt.customer_summary as select region from stage.customers_:month group by region", [{**MACRO("month"), "inline": True}],
     ("create", [], ["rpt.customer_summary"], ["stage.customers_#month#"])),
    # SAS: &lib..&tbl lowers to :lib.:tbl; a whole table name from a macro variable.
    ("select * from stage.a x join :lib.:tbl b on x.id = b.id", [MACRO("lib"), MACRO("tbl")], ("select", ["stage.a"], [], ["#lib#.#tbl#"])),
    ("select * from :dsn", [MACRO("dsn")], ("select", [], [], ["#dsn#"])),
    # No flag and no macro scope: a word with a :name in it is still dynamic, never `orders_`.
    ("SELECT * FROM orders_:region o JOIN :schema.items i ON 1 = 1", [], ("select", [], [], [":schema.items", "orders_:region"])),
    ("SELECT * FROM stage.customers_:month", None, ("select", [], [], ["stage.customers_:month"])),
    # Bind parameters, host variables and casts are not substitutions.
    ("SELECT a::int FROM t WHERE id = :id", [BIND("id")], ("select", ["t"], [], [])),
    ("select count(*) into :n trimmed from mart.region_summary", [], ("select", ["mart.region_summary"], [], [])),
    ("CREATE OR REPLACE TEMP VIEW recent (id) AS SELECT id FROM orders", [], ("create", ["orders"], ["recent"], [])),
    ("CREATE TABLE IF NOT EXISTS t2 (a int, b int)", [], ("create", [], ["t2"], [])),
    ("CREATE TABLE t3 AS (SELECT * FROM t4)", [], ("create", ["t4"], ["t3"], [])),
])
def test_sql_tables_with_inline_parameters(sql, params, expected):
    found = behavior_store.sql_tables(sql, params) if params is not None else behavior_store.sql_tables(sql)
    assert (found["statement"], found["reads"], found["writes"], found["dynamic"]) == expected


def test_mark_inline_rewrites_only_substitutions():
    sql = "SELECT * FROM t_:region_2024 JOIN :tbl USING (id) WHERE a = :a AND b = :region2 AND c::text = ':tbl'"
    params = [INLINE("region"), INLINE("tbl"), BIND("a"), BIND("region2")]
    assert behavior_store.mark_inline(sql, params) == "SELECT * FROM t_#region#_2024 JOIN #tbl# USING (id) WHERE a = :a AND b = :region2 AND c::text = '#tbl#'"
    assert behavior_store.mark_inline(sql, [BIND("a")]) == sql


def test_summary_lists_inline_parameters_and_dynamic_tables():
    query = {"op": "query", "at": [1, 2], "sql": "SELECT o.id FROM orders_:region o JOIN :tbl t ON t.id = o.id WHERE o.id = :id",
             "params": [INLINE("region"), INLINE("tbl", "variables"), BIND("id")], "datasource": "dsn"}
    s = behavior_store.summarize([{"id": "bhv_q", "language": "cfml", "kind": "page", "body": [query]}])
    [q] = s["queries"]
    assert q["params"] == ["region", "tbl", "id"] and q["inline_params"] == ["region", "tbl"]
    assert q["reads"] == [] and q["dynamic"] == ["#tbl#", "orders_#region#"] and q["unit_kind"] == "page"
    assert q["sql"] == query["sql"], "the SQL is reported as the pack wrote it"
    assert s["tables"] == {"reads": [], "writes": [], "dynamic": ["#tbl#", "orders_#region#"]}
    assert s["unit_kinds"] == {"page": 1}


# ------------------------------------------------------------------ the real packs


def _units(out):
    return [{**o.payload, "id": f"bhv_{i}"} for i, o in enumerate(out.observations) if o.kind == "behavior"]


def _from_substitution(table: str, sql: str) -> bool:
    """Whether `table` runs straight into a substitution in `sql`, so a scanner cut it short."""
    return re.search(rf"(?<![\w.]){re.escape(table)}[:#&]", sql, re.I) is not None


def test_the_real_packs_lower_substitutions_the_store_keeps_dynamic():
    from repo_intel.platforms.cfml.source import CfmlSource
    from repo_intel.platforms.sas import SasSource

    cfm = (b'<cfquery name="q" datasource="dsn">\nSELECT o.id FROM orders_#url.region# o JOIN #tbl# t ON t.id = o.id\n'
           b' WHERE o.id = <cfqueryparam value="#url.id#" cfsqltype="cf_sql_integer">\n</cfquery>\n'
           b'<cfscript>\nq2 = queryExecute("SELECT * FROM archive_#form.year#_orders WHERE x = :x", {x: 1});\n</cfscript>\n')
    sas = (b"proc sql;\n  create table work.out as select * from stage.customers_&month a join &lib..&tbl b on a.id = b.id;\n"
           b"  select * from &dsn;\nquit;\n")
    cf = behavior_store.summarize(_units(CfmlSource().extract("app/report.cfm", cfm, "0" * 64)))
    assert cf["tables"] == {"reads": [], "writes": [], "dynamic": ["#tbl#", "archive_#year#_orders", "orders_#region#"]}
    sa = behavior_store.summarize(_units(SasSource().extract("sas/load.sas", sas, "1" * 64)))
    assert sa["tables"] == {"reads": [], "writes": ["work.out"], "dynamic": ["#dsn#", "#lib#.#tbl#", "stage.customers_#month#"]}


@pytest.mark.parametrize("fixture", ["F14-mainframe-sas", "F15-legacy-web-warehouse", "F20-coldfusion-app", "F21-sas-analytics"])
def test_every_unit_of_a_real_fixture_summarises_without_substituted_names(fixture, tmp_path):
    """Compile the fixture with the real packs and summarise every owner's behaviour."""
    from repo_intel.compile.fixtures import materialize
    from repo_intel.compile.pipeline import compile_commit
    from repo_intel.storage.store import ObjectStore

    shas = materialize(fixture, tmp_path / "repo")
    store = ObjectStore(tmp_path / "store", "t")
    manifest = compile_commit(store, tmp_path / "repo", REPO_ID, shas[0]).manifest
    index = behavior_store.index(load_shards(store, manifest).values())
    assert index, f"{fixture} has behaviour"
    queries = []
    for units in index.values():
        summary = behavior_store.summarize(units)
        assert summary["units"] == len(units)
        queries += summary["queries"]
    for q in queries:
        for table in q["reads"] + q["writes"]:
            assert not _from_substitution(table, q["sql"]), f"{table!r} is cut from a substitution in {q['sql']!r}"
            assert not set(table) & set(":#&"), table
    if fixture == "F14-mainframe-sas":  # from stage.customers_&month
        [q] = [q for q in queries if "customers_" in q["sql"]]
        assert q["dynamic"] == ["stage.customers_#month#"] and q["writes"] == ["rpt.customer_summary"] and q["reads"] == []
