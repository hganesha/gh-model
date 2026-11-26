"""The solution layer (docs/solution-ir-contract.md, W5): the manifest, loading member
bundles, the merged view, identity across members, catalogue dumps and `rkp solution`.

The end-to-end tests use the base of the F23 golden fixture, benchmarks/fixtures/F23-solution-multi-repo:
two repositories (a web front end and the orders service it calls) and the orders database's catalogue.
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from repo_intel import platforms
from repo_intel.compile.contracts import entity_id
from repo_intel.compile.fixtures import FIXTURES_DIR
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.solution import MemberData, SolutionError, SolutionView, nsid, parse_manifest, resolve, split
from repo_intel.solution.identity import call_path, dataset_key, query_method, relational_key

FIXTURE = FIXTURES_DIR / "F23-solution-multi-repo" / "base"
CATALOG = FIXTURE / ".rkp" / "inputs" / "catalog"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "PATH": os.environ.get("PATH", ""),
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false", *args], env=_GIT_ENV, check=True, capture_output=True)


def _repository(source: Path, dest: Path) -> Path:
    shutil.copytree(source, dest)
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "base")
    return dest


@pytest.fixture(scope="module")
def shop(tmp_path_factory):
    """The F23 solution laid out as an owner would: the solution root holding two
    repositories, each compiled into its own bundle on this machine."""
    from repo_intel.knowledge import bundle

    root = tmp_path_factory.mktemp("shop") / "solution"
    shutil.copytree(FIXTURE / ".rkp", root / ".rkp")
    for member in ("web", "service"):
        bundle.compile_bundle(_repository(FIXTURE / member, root / member))
    return root


@pytest.fixture(scope="module")
def shop_links(shop):
    from repo_intel.solution import commands

    return commands.links(shop)


def _manifest(**overrides):
    document = {"schema": "rkp-solution/1", "name": "shop", "members": [{"id": "web", "path": "web", "role": "web"}], "inputs": []}
    document.update(overrides)
    return json.dumps(document)


# ------------------------------------------------------------------ the manifest


def test_a_valid_manifest_parses():
    m = parse_manifest((FIXTURE / ".rkp" / "solution.json").read_text())
    assert m.schema_ == "rkp-solution/1" and m.name == "shop"
    assert [(x.id, x.role, x.kind, x.source, x.database) for x in m.members] == [("web", "web", "repository", "web", None), ("orders", "service", "repository", "service", "shopdb")]
    assert m.inputs == [".rkp/inputs/catalog/*.json", ".rkp/inputs/catalog/*.csv"]
    delivered = parse_manifest(_manifest(members=[{"id": "reports", "bundle": "../delivered/reports/.rkp", "role": "batch"}]))
    member = delivered.member("reports")
    assert member.kind == "bundle" and member.directory(Path("/s")) == Path("/s/../delivered/reports")


@pytest.mark.parametrize("change, message", [
    ({"schema": "rkp-solution/2"}, "schema must be 'rkp-solution/1'"),
    ({"members": []}, "members"),
    ({"members": [{"id": "web", "path": "a", "role": "web"}, {"id": "web", "path": "b", "role": "web"}]}, "member ids must be unique: web"),
    ({"members": [{"id": "inputs", "path": "a", "role": "web"}]}, "reserved"),
    ({"members": [{"id": "Web", "path": "a", "role": "web"}]}, "lower-case"),
    ({"members": [{"id": "web", "path": "a", "bundle": "b/.rkp", "role": "web"}]}, "exactly one of 'path'"),
    ({"members": [{"id": "web", "role": "web"}]}, "exactly one of 'path'"),
    ({"members": [{"id": "web", "path": "/srv/web", "role": "web"}]}, "relative path"),
    ({"members": [{"id": "web", "path": "C:/web", "role": "web"}]}, "relative path"),
    ({"members": [{"id": "web", "path": "..\\web", "role": "web"}]}, "relative path"),
    ({"members": [{"id": "web", "bundle": "exports/web", "role": "web"}]}, "must name a .rkp directory"),
    ({"members": [{"id": "web", "path": "a", "role": "Web Front End"}]}, "role"),
    ({"members": [{"id": "web", "path": "a", "role": "web", "database": "shop db; drop"}]}, "database name"),
    ({"members": [{"id": "web", "path": "a", "role": "web", "url": "https://example.invalid"}]}, "Extra inputs are not permitted"),
    ({"members": [{"id": "a", "path": "web", "role": "web"}, {"id": "b", "bundle": "web/.rkp", "role": "web"}]}, "two members name the same bundle"),
    ({"inputs": ["inputs/catalog/x.json"]}, "must be under .rkp/inputs/"),
    ({"inputs": [".rkp/inputs/../solution.json"]}, "must be under .rkp/inputs/"),
    ({"inputs": [".rkp/inputs/a.json", ".rkp/inputs/a.json"]}, "listed twice"),
    ({"name": ""}, "name"),
])
def test_invalid_manifests_say_why(change, message):
    with pytest.raises(SolutionError, match="solution.json") as error:
        parse_manifest(_manifest(**change))
    assert message in str(error.value)


def test_manifest_must_be_a_json_object(tmp_path):
    from repo_intel.solution import load_manifest

    with pytest.raises(SolutionError, match="not valid JSON"):
        parse_manifest("{members")
    with pytest.raises(SolutionError, match="JSON object"):
        parse_manifest("[]")
    with pytest.raises(SolutionError, match="no .rkp/solution.json here"):
        load_manifest(tmp_path)


# ------------------------------------------------------------------ the merged view


def _row(repository, kind, natural_key, name=None, evidence="ev", **attributes):
    return {"id": entity_id(repository, kind, natural_key), "kind": kind, "natural_key": natural_key, "name": name or str(natural_key.get("name", "")), "attributes": attributes, "evidence_ids": [f"{evidence}_{kind}_{name or natural_key.get('name', '')}"]}


def _member(member_id, rows, role="service", database=None, catalog=False, calls=None, assertions=()):
    repository = f"local/{member_id}"
    entities = {}
    for kind, key, attributes in rows:
        row = _row(repository, kind, key, **attributes)
        entities[row["id"]] = row
    return MemberData(
        id=member_id, role=role, source=member_id, repository_id=repository, entities=entities,
        assertions={a["id"]: a for a in assertions}, database=database, is_catalog=catalog,
        exports=None if calls is None else {"operations": [], "unresolved_calls": calls},
    )


def _id(member: MemberData, kind, key):
    return nsid(member.id, entity_id(member.repository_id, kind, key))


def test_the_view_namespaces_entity_ids_by_member_and_is_read_only():
    table = ("Table", {"datastore": "default", "name": "orders"}, {})
    field = ("Field", {"table": "orders", "name": "id"}, {})
    a = _member("web", [table])
    b_rows = _member("orders", [table, field])
    t, f = (_id(b_rows, *x[:2]) for x in (table, field))
    contains = {"id": "as_1", "subject": split(t)[1], "predicate": "CONTAINS", "object": split(f)[1], "evidence_ids": ["ev"]}
    b = _member("orders", [table, field], assertions=[contains])
    view = SolutionView([a, b])
    ids = [e["id"] for e in view.entities("Table")]
    assert ids == [_id(a, *table[:2]), t] and ids[0] != ids[1], "the same natural key in two members is two entities"
    row = view.entity(t)
    assert row["member"] == "orders" and row["local_id"] == split(t)[1] and row["natural_key"] == {"datastore": "default", "name": "orders"}
    assert view.find("Table", {"datastore": "default", "name": "orders"}) == ids
    [edge] = view.assertions(t)
    assert (edge["subject"], edge["object"], edge["member"]) == (t, f, "orders")
    assert view.assertions(_id(a, *table[:2])) == [] and view.assertions("nobody:x") == []
    assert view.counts() == {"web": {"Table": 1}, "orders": {"Field": 1, "Table": 1}}
    row["name"] = "changed"
    edge["subject"] = "changed"
    assert view.entity(t)["name"] == "orders" and view.assertions(t)[0]["subject"] == t
    assert view.entity("orders:missing") is None
    with pytest.raises(ValueError):
        split("no-member-here")
    with pytest.raises(ValueError, match="duplicate member"):
        SolutionView([a, a])


# ------------------------------------------------------------------ identity rules


def _rules(resolution, rule):
    return [(r["from"], r["to"]) for r in resolution.links + resolution.candidates if r["rule"] == rule]


def test_same_table_needs_the_same_database_schema_and_name():
    catalog = _member("inputs", [
        ("Table", {"datastore": "default", "name": "orders"}, {"database": "shopdb", "schema": "public"}),
        ("Table", {"datastore": "default", "name": "audit.Order_Events"}, {"database": "shopdb", "schema": "audit"}),
        ("DataSchema", {"kind": "view", "name": "order_summary"}, {"database": "shopdb", "schema": "public"}),
    ], role="inputs", catalog=True)
    service = _member("orders", [
        ("Table", {"datastore": "default", "name": "ORDERS"}, {}),  # SQL folds unquoted names
        ("Table", {"datastore": "default", "name": "audit.order_events"}, {}),
        ("Table", {"datastore": "default", "name": "dbo.order_summary"}, {}),  # a view is read like a table
        ("Table", {"datastore": "default", "name": "report_{date}"}, {}),  # named at run time: never matched
    ], database="shopdb")
    other_db = _member("billing", [("Table", {"datastore": "default", "name": "warehouse.public.orders"}, {})], database="billing")
    view = SolutionView([service, other_db, catalog])
    r = resolve(view)
    links = {(x["from"], x["to"]) for x in r.links if x["rule"] == "same_table"}
    assert links == {
        (_id(service, "Table", {"datastore": "default", "name": "ORDERS"}), _id(catalog, "Table", {"datastore": "default", "name": "orders"})),
        (_id(service, "Table", {"datastore": "default", "name": "audit.order_events"}), _id(catalog, "Table", {"datastore": "default", "name": "audit.Order_Events"})),
        (_id(service, "Table", {"datastore": "default", "name": "dbo.order_summary"}), _id(catalog, "DataSchema", {"kind": "view", "name": "order_summary"})),
    }, "the catalogue is canonical; warehouse.public.orders is in another database"
    link = next(x for x in r.links if x["rule"] == "same_table" and x["to_name"] == "orders")
    assert link["predicate"] == "ALIAS_OF" and link["evidence_class"] == "E3" and link["resolution"] == "exact"
    assert link["qualifiers"] == {"database": "shopdb", "schema": None, "name": "orders"}
    assert {e["member"] for e in link["evidence"]} == {"orders", "inputs"}
    assert not r.candidates
    canonical = _id(catalog, "Table", {"datastore": "default", "name": "orders"})
    assert r.canonical(_id(service, "Table", {"datastore": "default", "name": "ORDERS"})) == canonical
    assert r.canonical(_id(other_db, "Table", {"datastore": "default", "name": "warehouse.public.orders"})).startswith("billing:")
    # ALIAS_OF is single-valued: every entity aliases at most one other.
    froms = [x["from"] for x in r.links if x["predicate"] == "ALIAS_OF"]
    assert len(froms) == len(set(froms))


def test_a_table_whose_database_is_unstated_is_only_a_candidate():
    catalog = _member("inputs", [("Table", {"datastore": "default", "name": "orders"}, {"database": "shopdb"})], role="inputs", catalog=True)
    web = _member("web", [("Table", {"datastore": "default", "name": "orders"}, {})])  # no `database` in the manifest
    r = resolve(SolutionView([web, catalog]))
    assert not r.links
    [c] = r.candidates
    assert c["rule"] == "table_database_unstated" and c["evidence_class"] == "E4" and c["resolution"] == "candidate"
    assert (c["from_member"], c["to_member"]) == ("web", "inputs")
    assert r.canonical(c["from"]) == c["from"], "a candidate is never merged"
    [u] = r.unmatched
    assert (u["member"], u["kind"], u["reference"], u["reason"]) == ("web", "table", "orders", "database_unstated")


def test_columns_follow_their_tables():
    key = {"datastore": "default", "name": "orders"}
    catalog = _member("inputs", [("Table", key, {"database": "shopdb"}), ("Field", {"table": "orders", "name": "status"}, {}), ("Field", {"table": "orders", "name": "id"}, {})], catalog=True)
    service = _member("orders", [("Table", key, {}), ("Field", {"table": "orders", "name": "Status"}, {}), ("Field", {"table": "orders", "name": "note"}, {})], database="shopdb")
    stranger = _member("crm", [("Table", key, {}), ("Field", {"table": "orders", "name": "status"}, {})], database="crm")
    r = resolve(SolutionView([service, stranger, catalog]))
    assert _rules(r, "same_column") == [(_id(service, "Field", {"table": "orders", "name": "Status"}), _id(catalog, "Field", {"table": "orders", "name": "status"}))]


def test_topics_and_sas_datasets_match_by_name():
    a = _member("producer", [
        ("EventTopic", {"name": "order-events"}, {}), ("EventTopic", {"name": "Audit"}, {}),
        ("Dataset", {"name": "SALES.Daily"}, {"platform": "sas"}), ("Dataset", {"name": "work.tmp"}, {"platform": "sas"}),
        ("Dataset", {"name": "scratch"}, {"platform": "sas"}), ("Dataset", {"name": "exports"}, {}),
    ])
    b = _member("consumer", [
        ("EventTopic", {"name": "order-events"}, {}), ("EventTopic", {"name": "audit"}, {}),
        ("Dataset", {"library": "sales", "name": "daily"}, {"platform": "sas"}), ("Dataset", {"name": "WORK.tmp"}, {"platform": "sas"}),
        ("Dataset", {"name": "scratch"}, {"platform": "sas"}), ("Dataset", {"name": "exports"}, {}),
    ])
    r = resolve(SolutionView([a, b]))
    assert _rules(r, "same_topic") == [(_id(b, "EventTopic", {"name": "order-events"}), _id(a, "EventTopic", {"name": "order-events"}))], "topic names are compared exactly"
    assert _rules(r, "same_dataset") == [(_id(b, "Dataset", {"library": "sales", "name": "daily"}), _id(a, "Dataset", {"name": "SALES.Daily"}))]
    # WORK and one-level SAS names are a program's scratch data; a one-level name elsewhere is a lead only.
    assert _rules(r, "dataset_library_unstated") == [(_id(b, "Dataset", {"name": "exports"}), _id(a, "Dataset", {"name": "exports"}))]
    assert all(link["evidence_class"] == "E3" for link in r.links)


def test_a_dotted_dataset_name_is_split_only_when_both_sides_are_sas():
    """A JCL DSN `MART.SALES` is not a SAS library and member: matching it to SAS
    `mart.sales` is a lead across platforms, never an exact link."""
    jcl = _member("jobs", [
        ("Dataset", {"name": "MART.SALES"}, {"platform": "jcl"}),
        ("Dataset", {"name": "PROD.ORDERS"}, {"platform": "jcl", "platforms": ["jcl", "sas"]}),  # also declared by SAS
        ("Dataset", {"name": "spark.events"}, {}),
    ], role="batch")
    sas = _member("reports", [
        ("Dataset", {"name": "mart.sales"}, {"platform": "sas"}),
        ("Dataset", {"name": "prod.orders"}, {"platform": "sas"}),
    ], role="batch")
    spark = _member("lake", [("Dataset", {"name": "spark.events"}, {})])
    r = resolve(SolutionView([jcl, sas, spark]))
    assert _rules(r, "same_dataset") == [(_id(sas, "Dataset", {"name": "prod.orders"}), _id(jcl, "Dataset", {"name": "PROD.ORDERS"}))], "both declared by SAS: exact"
    found = {(c["from_name"], c["to_name"]): c for c in r.candidates if c["rule"] == "dataset_name_only"}
    assert set(found) == {("mart.sales", "MART.SALES"), ("spark.events", "spark.events")}
    assert found[("mart.sales", "MART.SALES")]["qualifiers"] == {"name": "mart.sales", "platforms": ["jcl", "sas"]}
    assert all(c["evidence_class"] == "E4" and c["resolution"] == "candidate" for c in found.values())
    assert r.canonical(_id(sas, "Dataset", {"name": "mart.sales"})) == _id(sas, "Dataset", {"name": "mart.sales"}), "a candidate is never merged"


_LOCAL_KEYS = (
    ("ConfigKey", {"name": "LOG_LEVEL"}, {}),
    ("ConfigKey", {"name": "API_URL"}, {"source": "env"}),
    ("ConfigKey", {"name": "date"}, {"source": "databricks_widget"}),
    ("ConfigKey", {"name": "libname:mart"}, {"source": "sas_libname"}),
    ("ConfigKey", {"name": "datasource:shopdsn"}, {"source": "cfml_datasource"}),
    ("ConfigKey", {"name": "sales/jdbc-password"}, {"source": "databricks_secret"}),
)


def _declared_in(member_id, key, path, evidence_class="E1", rows=()):
    """A member whose ConfigKey `key` an artifact at `path` declares at `evidence_class`."""
    repository = f"local/{member_id}"
    artifact = {"path": path}
    defined = {"id": "as_defined", "subject": entity_id(repository, "ConfigKey", key), "predicate": "DEFINED_IN", "object": entity_id(repository, "Artifact", artifact), "evidence_class": evidence_class, "evidence_ids": ["ev"]}
    return _member(member_id, [*rows, ("ConfigKey", key, {"administered": True}), ("Artifact", artifact, {"name": path.rsplit("/", 1)[-1]})], assertions=[defined])


def test_config_keys_are_one_only_where_a_side_is_authoritative():
    """`LOG_LEVEL` read by two services may be two settings, and so may two notebooks' widget
    `date`, two programs' libref `mart` or two servers' datasource `shopdsn`: the same kind of
    source is not proof. The solution's inputs, a `config` or `database` member, an E1
    declaration from an export under `.rkp/inputs/`, or a source naming a namespace every
    member shares (a Databricks secret `scope/key`) settles it."""
    web = _member("web", list(_LOCAL_KEYS), role="web")
    service = _member("orders", list(_LOCAL_KEYS))
    r = resolve(SolutionView([web, service]))
    secret = {"name": "sales/jdbc-password"}
    assert _rules(r, "same_config_key") == [(_id(service, "ConfigKey", secret), _id(web, "ConfigKey", secret))], "only the shared secret namespace"
    assert all(x["evidence_class"] == "E3" for x in r.links)
    leads = {c["from_name"]: c for c in r.candidates}
    assert set(leads) == {"LOG_LEVEL", "API_URL", "date", "libname:mart", "datasource:shopdsn"}
    for lead in leads.values():
        assert (lead["rule"], lead["evidence_class"], lead["resolution"], lead["from_member"], lead["to_member"]) == ("config_key_name_only", "E4", "candidate", "orders", "web")
        assert r.canonical(lead["from"]) == lead["from"]
    assert leads["LOG_LEVEL"]["qualifiers"] == {"name": "LOG_LEVEL"}
    assert leads["API_URL"]["qualifiers"] == {"name": "API_URL"}, "an environment variable is the default source"
    assert leads["date"]["qualifiers"] == {"name": "date", "source": "databricks_widget"}
    assert leads["libname:mart"]["qualifiers"] == {"name": "libname:mart", "source": "sas_libname"}
    assert leads["datasource:shopdsn"]["qualifiers"] == {"name": "datasource:shopdsn", "source": "cfml_datasource"}
    # Different sources are kept apart in the qualifiers.
    mixed = resolve(SolutionView([_member("web", [("ConfigKey", {"name": "date"}, {"source": "databricks_widget"})]), _member("orders", [("ConfigKey", {"name": "date"}, {"source": "sas_config"})])]))
    assert [c["qualifiers"] for c in mixed.candidates] == [{"name": "date", "sources": ["databricks_widget", "sas_config"]}]
    # A member that declares configuration for the solution makes every reader one key with it.
    log_level = {"name": "LOG_LEVEL"}
    for authority in (_member("settings", [("ConfigKey", log_level, {})], role="config"), _member("inputs", [("ConfigKey", log_level, {})], role="inputs", catalog=True)):
        r = resolve(SolutionView([web, service, authority]))
        target = _id(authority, "ConfigKey", log_level)
        assert sorted(_rules(r, "same_config_key")) == sorted([
            (_id(service, "ConfigKey", secret), _id(web, "ConfigKey", secret)),
            *([(_id(web, "ConfigKey", log_level), target), (_id(service, "ConfigKey", log_level), target)] if authority.is_catalog else
              [(_id(service, "ConfigKey", log_level), _id(web, "ConfigKey", log_level)), (target, _id(web, "ConfigKey", log_level))]),
        ])
        assert not [c for c in r.candidates if c["from_name"] == "LOG_LEVEL"], "joined through the authority, so no lead between the readers"
        assert all(x["evidence_class"] == "E3" for x in r.links)


def test_a_config_key_declared_by_an_export_under_rkp_inputs_is_authoritative():
    """A ColdFusion server's `neo-datasource.xml`, committed under `.rkp/inputs/cfml/`,
    declares its datasources at E1: code naming the same datasource is one key with it."""
    shopdsn = {"name": "datasource:shopdsn"}
    code = ("ConfigKey", shopdsn, {"source": "cfml_datasource"})
    web, service = _member("web", [code], role="web"), _member("orders", [code])
    admin = _declared_in("admin", shopdsn, ".rkp/inputs/cfml/neo-datasource.xml")
    r = resolve(SolutionView([web, service, admin]))
    target = _id(admin, "ConfigKey", shopdsn)
    assert sorted(_rules(r, "same_config_key")) == sorted([(_id(service, "ConfigKey", shopdsn), _id(web, "ConfigKey", shopdsn)), (target, _id(web, "ConfigKey", shopdsn))])
    assert all(x["evidence_class"] == "E3" and x["resolution"] == "exact" for x in r.links)
    assert r.candidates == []
    assert r.canonical(target) == r.canonical(_id(service, "ConfigKey", shopdsn))
    # A declaration outside `.rkp/inputs/`, or one under it at less than E1, is not an authority.
    for other in (_declared_in("admin", shopdsn, "deploy/neo-datasource.xml"), _declared_in("admin", shopdsn, ".rkp/inputs/cfml/neo-datasource.xml", evidence_class="E2")):
        r = resolve(SolutionView([web, service, other]))
        assert r.links == []
        assert {(c["rule"], c["evidence_class"]) for c in r.candidates} == {("config_key_name_only", "E4")} and len(r.candidates) == 3


def test_name_only_matches_across_kinds_of_store_are_candidates():
    a = _member("orders", [("Table", {"datastore": "default", "name": "orders"}, {}), ("EventTopic", {"name": "invoices"}, {})], database="shopdb")
    b = _member("batch", [("Dataset", {"name": "sales.orders"}, {"platform": "sas"}), ("StorageResource", {"name": "invoices"}, {})])
    r = resolve(SolutionView([a, b]))
    assert not r.links
    found = {(c["from_name"], c["to_name"]): c for c in r.candidates}
    assert set(found) == {("sales.orders", "orders"), ("invoices", "invoices")}
    assert found[("sales.orders", "orders")]["qualifiers"] == {"name": "orders", "stores": ["dataset", "relational"]}
    assert all(c["evidence_class"] == "E4" and c["resolution"] == "candidate" and c["rule"] == "name_only_cross_store" for c in r.candidates)
    assert r.groups == {}


def _endpoint(method, route):
    return ("Endpoint", {"component_root": "", "protocol": "http", "method": method, "route": route}, {})


def test_http_calls_link_to_the_endpoint_another_member_serves():
    caller = entity_id("local/web", "Component", {"root": ""})
    calls = [
        {"caller": caller, "path": "/api/orders/{}", "evidence_ids": ["ev_get"]},  # one endpoint serves it
        {"caller": caller, "path": "/api/orders", "method": "post", "evidence_ids": ["ev_post"]},  # method stated
        {"caller": caller, "path": "/api/orders", "evidence_ids": ["ev_list"]},  # two endpoints, no method
        {"caller": caller, "path": "{}/api/orders/{}/items", "evidence_ids": ["ev_base"]},  # a base URL read at run time
        {"caller": caller, "path": "https://shop.example.invalid/gateway/api/invoices", "evidence_ids": ["ev_suffix"]},
        {"caller": caller, "path": "{}", "evidence_ids": ["ev_dynamic"]},
        {"caller": caller, "path": "/api/customers", "evidence_ids": ["ev_none"]},
        {"caller": caller, "path": "/api/local", "evidence_ids": ["ev_own"]},
    ]
    web = _member("web", [("Component", {"root": ""}, {}), _endpoint("GET", "/api/local")], calls=calls)
    orders = _member("orders", [_endpoint("GET", "/api/orders"), _endpoint("POST", "/api/orders"), _endpoint("GET", "/api/orders/{order_id}"), _endpoint("ANY", "/api/orders/:id/items")], calls=[])
    billing = _member("billing", [_endpoint("GET", "/api/invoices")], calls=[])
    r = resolve(SolutionView([web, orders, billing]))
    me = nsid("web", caller)
    e = lambda m, method, route: _id(m, "Endpoint", {"component_root": "", "protocol": "http", "method": method, "route": route})
    assert {(x["rule"], x["to"]) for x in r.links} == {
        ("endpoint_path_unique", e(orders, "GET", "/api/orders/{order_id}")),
        ("endpoint_method_path", e(orders, "POST", "/api/orders")),
        ("endpoint_path_unique", e(orders, "ANY", "/api/orders/:id/items")),
    }
    assert all(x["from"] == me and x["predicate"] == "CONSUMES" and x["evidence_class"] == "E3" for x in r.links)
    unique = next(x for x in r.links if x["to"] == e(orders, "GET", "/api/orders/{order_id}"))
    assert unique["qualifiers"] == {"path": "/api/orders/{}", "method": "unstated"}
    assert {"member": "web", "evidence_ids": ["ev_get"]} in unique["evidence"]
    assert {(x["rule"], x["to"]) for x in r.candidates} == {
        ("endpoint_method_unstated", e(orders, "GET", "/api/orders")),
        ("endpoint_method_unstated", e(orders, "POST", "/api/orders")),
        ("endpoint_path_suffix", e(billing, "GET", "/api/invoices")),
    }
    assert {(u["reference"], u["reason"]) for u in r.unmatched} == {("{}", "dynamic_url"), ("/api/customers", "no_endpoint"), ("/api/local", "no_endpoint")}, "a member's own endpoint is not a cross-member link"
    [coverage] = [c for c in r.coverage if c["domain"] == "solution_http_calls"]
    assert (coverage["scope"], coverage["numerator"], coverage["denominator"]) == ("web", 3, 8)
    assert coverage["detail"] == {"available": True, "candidates": 2, "unmatched": 2, "dynamic": 1}


def test_repeated_calls_pool_their_evidence_and_missing_exports_are_reported():
    caller = entity_id("local/web", "Component", {"root": ""})
    calls = [{"caller": caller, "path": "/api/orders/{}", "evidence_ids": [f"ev_{i}"]} for i in range(2)]
    web = _member("web", [("Component", {"root": ""}, {})], calls=calls)
    orders = _member("orders", [_endpoint("GET", "/api/orders/{id}")])  # exports not available
    r = resolve(SolutionView([web, orders]))
    [link] = r.links
    assert {"member": "web", "evidence_ids": ["ev_0", "ev_1"]} in link["evidence"]
    coverage = {c["scope"]: c for c in r.coverage if c["domain"] == "solution_http_calls"}
    assert coverage["orders"]["detail"]["available"] is False and coverage["orders"]["denominator"] == 0


def _cfc_routes():
    """The routes the CFML pack gives a component's remote methods, read from its own output."""
    src = (b'<cfcomponent>\n'
           b'<cffunction name="getOrder" access="remote" returntype="struct"><cfargument name="id"><cfreturn {}></cffunction>\n'
           b'<cffunction name="saveOrder" access="remote"><cfargument name="order"><cfreturn true></cffunction>\n'
           b'<cffunction name="helper" access="private"><cfreturn 1></cffunction>\n'
           b'</cfcomponent>\n')
    path = "services/OrderService.cfc"
    [extractor] = [x for x in platforms.packs()["cfml"].extractors if x.matches(path)]
    result = extractor.extract(path, src, hashlib.sha256(src).hexdigest())
    return sorted(o.payload["natural_key"]["route"] for o in result.observations if o.kind == "entity" and o.payload["kind"] == "Endpoint")


def test_cfc_remote_methods_are_separate_endpoints():
    routes = _cfc_routes()
    assert routes == ["/services/OrderService.cfc?method=getOrder", "/services/OrderService.cfc?method=saveOrder"]
    caller = entity_id("local/web", "Component", {"root": ""})
    calls = [
        {"caller": caller, "path": "/services/OrderService.cfc?method=getOrder", "evidence_ids": ["ev_get"]},
        {"caller": caller, "path": "https://shop.example.invalid/services/OrderService.cfc?method=SAVEORDER&id=1", "evidence_ids": ["ev_save"]},  # ColdFusion ignores case
        {"caller": caller, "path": "/gateway/services/OrderService.cfc?method=getOrder", "evidence_ids": ["ev_prefix"]},
        {"caller": caller, "path": "/services/OrderService.cfc?method=deleteOrder", "evidence_ids": ["ev_delete"]},
        {"caller": caller, "path": "/services/OrderService.cfc?method={}", "evidence_ids": ["ev_dynamic"]},
        {"caller": caller, "path": "/services/OrderService.cfc", "evidence_ids": ["ev_bare"]},
        {"caller": caller, "path": "/api/orders?method=list", "evidence_ids": ["ev_rest"]},  # a REST endpoint: the query is its input
    ]
    web = _member("web", [("Component", {"root": ""}, {})], calls=calls)
    legacy = _member("legacy", [_endpoint("ANY", route) for route in routes] + [_endpoint("GET", "/api/orders")], calls=[])
    r = resolve(SolutionView([web, legacy]))
    e = lambda method, route: _id(legacy, "Endpoint", {"component_root": "", "protocol": "http", "method": method, "route": route})
    get, save = e("ANY", routes[0]), e("ANY", routes[1])
    links = {(x["rule"], x["to"], tuple(sorted(i for ev in x["evidence"] if ev["member"] == "web" for i in ev["evidence_ids"]))) for x in r.links}
    assert links == {
        ("endpoint_path_unique", get, ("ev_get",)),
        ("endpoint_path_unique", save, ("ev_save",)),
        ("endpoint_path_unique", e("GET", "/api/orders"), ("ev_rest",)),
    }, "each call reaches only the remote method it names"
    assert next(x for x in r.links if x["to"] == save)["qualifiers"] == {"path": "/services/OrderService.cfc", "method": "unstated", "query_method": "saveorder"}
    assert {(c["rule"], c["to"]) for c in r.candidates} == {("endpoint_path_suffix", get)}
    assert {(u["reference"], u["reason"]) for u in r.unmatched} == {
        ("/services/OrderService.cfc?method=deleteorder", "no_endpoint"),
        ("/services/OrderService.cfc?method={}", "dynamic_url"),
        ("/services/OrderService.cfc", "no_endpoint"),
    }


def test_key_helpers():
    m = MemberData(id="m", role="r", source="m", repository_id="r", database="Shop")
    assert relational_key(m, {"kind": "Table", "natural_key": {"datastore": "default", "name": "[dbo].[Orders]"}, "attributes": {}}) == ("shop", None, "orders")
    assert relational_key(m, {"kind": "Table", "natural_key": {"datastore": "dw", "name": "x.y"}, "attributes": {}}) == ("dw", "x", "y")
    assert relational_key(m, {"kind": "DataSchema", "natural_key": {"kind": "copybook", "name": "x"}, "attributes": {}}) is None
    assert relational_key(m, {"kind": "Table", "natural_key": {"datastore": "default", "name": "&lib..sales"}, "attributes": {}}) is None
    assert dataset_key({"natural_key": {"name": "Lib.Member"}, "attributes": {"platform": "sas"}}) == ("lib", "member")
    assert dataset_key({"natural_key": {"name": "Lib.Member"}, "attributes": {"platforms": ["jcl", "sas"]}}) == ("lib", "member")
    assert dataset_key({"natural_key": {"name": "MART.SALES"}, "attributes": {"platform": "jcl"}}) == (None, "mart.sales"), "a JCL DSN is not library.member"
    assert query_method("/s/OrderService.cfc?method=getOrder&x=1") == ("getorder", False)
    assert query_method("/s/OrderService.cfc?id=1&method={}") == (None, True)
    assert query_method("/api/orders") == (None, False)
    assert dataset_key({"natural_key": {"name": "s3://bucket/key.csv"}, "attributes": {}}) == (None, "s3://bucket/key.csv")
    assert dataset_key({"natural_key": {"name": "&src..sales"}, "attributes": {}}) is None
    assert call_path("/api/orders?x=1") == "/api/orders"
    assert call_path("api/orders") == "/api/orders"
    assert call_path("https://h.example.invalid") is None
    assert call_path("{}/{}") is None


# ------------------------------------------------------------------ the catalogue pack


def _extract(path: str, data: bytes):
    from repo_intel.platforms.catalog import PACK

    [extractor] = PACK.extractors
    assert extractor.matches(path)
    return extractor.extract(path, data, hashlib.sha256(data).hexdigest())


def _entities(result, kind=None):
    return [o.payload for o in result.observations if o.kind == "entity" and (kind is None or o.payload["kind"] == kind)]


def _relations(result):
    return [(o.payload["subject"]["name"], o.payload["predicate"], o.payload["object"]["kind"], o.payload["object"]["name"], o.payload["object"].get("declare", True)) for o in result.observations if o.kind == "relation"]


def _constructs(result):
    counts = {}
    for o in result.observations:
        if o.kind == "construct":
            seen, modelled = counts.get(o.payload["construct"], (0, 0))
            counts[o.payload["construct"]] = (seen + 1, modelled + o.payload["modelled"])
    return counts


def test_the_catalog_pack_is_registered_and_valid():
    pack = platforms.packs()["catalog"]
    assert platforms.validate_pack(pack) == [] and pack.prefix == "db"
    assert {t.name: (t.core.value, t.fidelity) for t in pack.terms} == {
        "db:Table": ("Table", "lossy"), "db:View": ("DataSchema", "lossy"), "db:Column": ("Field", "lossy"),
        "db:Routine": ("Program", "lossy"), "db:Role": ("Role", "exact"), "db:Grant": ("Permission", "lossy"),
    }
    [extractor] = pack.extractors
    assert extractor.id == "catalog.dump"
    assert extractor.matches(".rkp/inputs/catalog/shop.csv") and extractor.matches(".rkp/inputs/catalog/shop.JSON")
    assert not extractor.matches(".rkp/inputs/catalog/old/shop.json") and not extractor.matches("db/catalog/shop.json") and not extractor.matches(".rkp/inputs/catalog/shop.sql")


def test_a_json_dump_gives_tables_views_columns_keys_routines_and_roles():
    data = (CATALOG / "shopdb.json").read_bytes()
    result = _extract(".rkp/inputs/catalog/shopdb.json", data)
    assert result.parsed
    tables = {e["natural_key"]["name"]: e for e in _entities(result, "Table")}
    assert set(tables) == {"orders", "customers", "audit.order_events"}, "pg_catalog is a system schema"
    assert tables["orders"]["natural_key"] == {"datastore": "default", "name": "orders"}
    assert tables["orders"]["attributes"] == {"database": "shopdb", "schema": "public", "table_type": "BASE TABLE", "platform": "catalog", "platform_kind": "db:Table", "fidelity": "lossy"}
    views = _entities(result, "DataSchema")
    assert {v["natural_key"]["name"] for v in views} == {"order_summary"} and {"kind": "view", "name": "order_summary"} in [v["natural_key"] for v in views]
    assert any(v["attributes"].get("reads") == ["orders"] for v in views)
    fields = {}
    for e in _entities(result, "Field"):
        fields.setdefault((e["natural_key"]["table"], e["natural_key"]["name"]), {}).update(e["attributes"])
    assert len(fields) == 10
    assert fields[("orders", "id")].items() >= {"type": "integer", "nullable": False, "position": 1, "primary_key": True}.items()
    assert fields[("orders", "customer_id")]["references"] == "customers.id"
    assert fields[("customers", "email")]["unique"] is True
    assert ("orders", "CONTAINS", "Field", "status", True) in _relations(result)
    assert ("order_summary", "CONTAINS", "Field", "orders", True) in _relations(result)
    [routine] = _entities(result, "Program")
    assert routine["natural_key"] == {"platform": "sql", "name": "close_stale_orders"}
    assert routine["attributes"]["parameters"] == ["older_than_days"] and routine["attributes"]["kind"] == "procedure"
    assert {r for r in _relations(result) if r[0] == "close_stale_orders"} == {
        ("close_stale_orders", "WRITES_TO", "Table", "orders", True), ("close_stale_orders", "READS_FROM", "Table", "orders", True),
        ("close_stale_orders", "WRITES_TO", "Table", "audit.order_events", True),
    }
    assert ("orders_service", "GRANTS", "Role", "orders_writer", True) in _relations(result)
    assert {r["natural_key"]["realm"] for r in _entities(result, "Role")} == {"shopdb"}
    assert _constructs(result) == {
        "db:table": (3, 3), "db:view": (1, 1), "db:view_dependency": (1, 1), "db:column": (10, 10), "db:primary_key": (2, 2),
        "db:unique_constraint": (1, 1), "db:foreign_key": (1, 1), "db:check_constraint": (1, 0), "db:routine": (1, 1),
        "db:routine_parameter": (1, 1), "db:routine_lineage": (1, 1), "db:role_membership": (2, 2), "db:sequence": (1, 0), "db:trigger": (1, 0),
    }
    # Every observation has a span into the file; what the catalogue declares is E1,
    # construct occurrences included (a dump is a declarative export).
    for o in result.observations:
        assert 0 <= o.evidence.span.start_byte < o.evidence.span.end_byte <= len(data)
        assert o.evidence_class == "E1", (o.kind, o.payload)
    assert any(o.kind == "construct" for o in result.observations)
    first = next(o for o in result.observations if o.kind == "entity" and o.payload["natural_key"].get("name") == "orders" and o.payload["kind"] == "Table")
    assert data[first.evidence.span.start_byte:first.evidence.span.end_byte].startswith(b'{"table_catalog": "shopdb", "table_schema": "public", "table_name": "orders"')


def test_definitions_and_defaults_are_never_copied():
    data = (CATALOG / "shopdb.json").read_bytes()
    text = json.dumps([o.row() for o in _extract(".rkp/inputs/catalog/shopdb.json", data).observations])
    for value in ("nextval", "'new'", "closed", "count(*)", "GROUP BY"):
        assert value not in text


def test_a_csv_dump_with_upper_case_headers_gives_roles_and_grants():
    data = (CATALOG / "shopdb.table_privileges.csv").read_bytes()
    result = _extract(".rkp/inputs/catalog/shopdb.table_privileges.csv", data)
    permissions = {p["natural_key"]["name"]: p for p in _entities(result, "Permission")}
    assert set(permissions) == {"SELECT on orders", "SELECT on customers", "INSERT on orders", "UPDATE on orders", "SELECT on order_summary", "SELECT on audit.order_events"}
    assert permissions["SELECT on audit.order_events"]["attributes"].items() >= {"privilege": "SELECT", "object_kind": "table", "object": "audit.order_events", "grantable": True}.items()
    roles = {r["natural_key"]["name"]: r for r in _entities(result, "Role")}
    assert roles["PUBLIC"]["attributes"]["pseudo"] is True and roles["orders_reader"]["natural_key"] == {"realm": "shopdb", "name": "orders_reader"}
    assert ("orders_writer", "GRANTS", "Permission", "UPDATE on orders", True) in _relations(result)
    assert _constructs(result) == {"db:table_grant": (6, 6)}
    line = next(o for o in result.observations if o.kind == "entity" and o.payload["name"] == "auditor")
    assert data[line.evidence.span.start_byte:line.evidence.span.end_byte] == b"postgres,auditor,shopdb,audit,order_events,SELECT,YES\n"
    assert line.evidence.span.start_line == 7


def test_csv_sections_are_known_by_name_or_header():
    columns = b"table_schema,table_name,column_name,data_type\nsales,orders,id,int\n"
    result = _extract(".rkp/inputs/catalog/export.csv", columns)
    assert [e["natural_key"] for e in _entities(result, "Field")] == [{"table": "sales.orders", "name": "id"}]
    # A columns file does not know whether its table is a table or a view: neither is declared.
    containers = {(o.payload["subject"]["kind"], o.payload["subject"].get("declare", True)) for o in result.observations if o.kind == "relation"}
    assert containers == {("Table", False), ("DataSchema", False)}
    assert not _entities(result, "Table")
    named = _extract(".rkp/inputs/catalog/crm.applicable_roles.csv", b"GRANTEE,ROLE_NAME\nalice,crm_admin\n")
    assert {r["natural_key"]["realm"] for r in _entities(named, "Role")} == {"crm"} and named.diagnostics == []
    by_header = _extract(".rkp/inputs/catalog/crm-roles.csv", b"GRANTEE,ROLE_NAME\nalice,crm_admin\n")
    assert _relations(by_header) == [("alice", "GRANTS", "Role", "crm_admin", True)]
    unknown = _extract(".rkp/inputs/catalog/notes.csv", b"a,b\n1,2\n")
    assert not unknown.observations and not unknown.parsed


def test_odd_dumps_are_reported_not_guessed():
    broken = _extract(".rkp/inputs/catalog/x.json", b'{"tables": [ {"table_name": "a"}, ')
    assert not broken.parsed and "not a catalogue dump" in broken.diagnostics[0].message
    two = json.dumps({"tables": [
        {"table_catalog": "a", "table_schema": "public", "table_name": "t", "table_type": "BASE TABLE"},
        {"table_catalog": "b", "table_schema": "public", "table_name": "t", "table_type": "BASE TABLE"},
    ]}).encode()
    result = _extract(".rkp/inputs/catalog/two.json", two)
    assert sorted(e["natural_key"]["datastore"] for e in _entities(result, "Table")) == ["a", "b"]
    assert any("2 databases" in d.message for d in result.diagnostics)
    dynamic = json.dumps({"database": "d", "routines": [{"routine_schema": "public", "routine_name": "f", "routine_type": "FUNCTION", "routine_definition": "EXECUTE 'DELETE FROM ' || tbl;"}]}).encode()
    result = _extract(".rkp/inputs/catalog/d.json", dynamic)
    assert not [r for r in _relations(result) if r[0] == "f"], "a table named at run time is not resolved"
    assert _constructs(result)["db:routine_lineage"] == (1, 0)
    unnamed = _extract(".rkp/inputs/catalog/grants.csv", b"grantee,table_schema,table_name,privilege_type\nbob,public,t,SELECT\n")
    assert {r["natural_key"]["realm"] for r in _entities(unnamed, "Role")} == {"catalog"}
    assert any("no database is named" in d.message for d in unnamed.diagnostics)
    bom = b"\xef\xbb\xbf" + b'{"database": "d", "tables": [{"table_name": "t\xc3\xa9", "table_type": "BASE TABLE"}]}'
    [t] = [o for o in _extract(".rkp/inputs/catalog/bom.json", bom).observations if o.kind == "entity"]
    assert bom[t.evidence.span.start_byte:t.evidence.span.end_byte] == b'{"table_name": "t\xc3\xa9", "table_type": "BASE TABLE"}'


def test_catalog_keys_are_the_keys_code_and_ddl_use():
    """A table described by the catalogue and created by DDL, or a procedure listed by the
    catalogue and defined in a script, is one entity in a compile."""
    from repo_intel.extractors.contracts_data import SqlDdl
    from repo_intel.extractors.sql_lineage import SqlScripts

    ddl = b"CREATE TABLE orders (id integer PRIMARY KEY, status text);\nCREATE TABLE audit.order_events (order_id integer);\nCREATE VIEW order_summary AS SELECT status FROM orders;\n"
    parsed = SqlDdl().extract("db/schema.sql", ddl, hashlib.sha256(ddl).hexdigest())
    proc = b"CREATE PROCEDURE close_stale_orders(older_than_days integer)\nLANGUAGE sql AS $$\nUPDATE orders SET status = 'closed';\n$$;\n"
    script = SqlScripts().extract("db/procs.sql", proc, hashlib.sha256(proc).hexdigest())
    catalog = _extract(".rkp/inputs/catalog/shopdb.json", (CATALOG / "shopdb.json").read_bytes())

    def keys(result, kinds):
        return {(o.payload["kind"], json.dumps(o.payload["natural_key"], sort_keys=True)) for o in result.observations if o.kind == "entity" and o.payload["kind"] in kinds}

    shared = keys(parsed, {"Table", "Field", "DataSchema"})
    assert shared and shared <= keys(catalog, {"Table", "Field", "DataSchema"})
    assert keys(script, {"Program"}) <= keys(catalog, {"Program"})


def test_catalog_output_passes_the_publish_gate_checks():
    for name in ("shopdb.json", "shopdb.table_privileges.csv"):
        result = _extract(f".rkp/inputs/catalog/{name}", (CATALOG / name).read_bytes())
        for e in _entities(result):
            assert platforms.check_platform_kind(e["kind"], e["attributes"]) is None
        for o in (o for o in result.observations if o.kind == "relation"):
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None


def test_a_repository_with_a_catalog_dump_compiles_it(tmp_path):
    """The pack runs in an ordinary compile: `.rkp/inputs/catalog/` is repository input."""
    from repo_intel.compile.pipeline import compile_commit, load_shards
    from repo_intel.storage.store import ObjectStore

    repo = tmp_path / "db"
    (repo / ".rkp" / "inputs").mkdir(parents=True)
    shutil.copytree(CATALOG, repo / ".rkp" / "inputs" / "catalog")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "catalogue")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    store = ObjectStore(tmp_path / "store", "t")
    result = compile_commit(store, repo, "github.com/1001", sha)
    assert result.manifest.status == "complete"
    entities = {e["id"]: e for s in load_shards(store, result.manifest).values() for e in s["entities"]}
    orders = entities[entity_id("github.com/1001", "Table", {"datastore": "default", "name": "orders"})]
    assert orders["attributes"]["platform_kind"] == "db:Table"
    assert entity_id("github.com/1001", "Permission", {"realm": "shopdb", "name": "SELECT on order_summary"}) in entities
    [coverage] = [c for c in store.get_json(result.manifest.coverage_ref) if c["domain"] == "platform_constructs"]
    assert (coverage["scope"], coverage["numerator"], coverage["denominator"]) == ("catalog", 30, 33)


# ------------------------------------------------------------------ loading the F23 solution


def test_check_loads_every_member_and_the_inputs(shop):
    from repo_intel.solution import commands

    report = commands.check(shop)
    assert report["ok"] and report["problems"] == [] and report["warnings"] == []
    members = {m["id"]: m for m in report["members"]}
    assert members["web"]["loaded"] and members["web"]["calls_available"] and members["web"]["repository_id"] == "local/web"
    assert members["orders"]["database"] == "shopdb" and members["orders"]["kinds"]["Endpoint"] == 3
    assert members["orders"]["detail"] == "complete", "compiled on this machine: read from its own cache"
    assert report["inputs"]["files"] == [".rkp/inputs/catalog/shopdb.json", ".rkp/inputs/catalog/shopdb.table_privileges.csv"]
    assert report["inputs"]["kinds"] == {"Artifact": 2, "DataSchema": 1, "Field": 10, "Permission": 6, "Program": 1, "Role": 5, "Table": 3}
    [coverage] = report["inputs"]["coverage"]
    assert (coverage["domain"], coverage["scope"], coverage["numerator"], coverage["denominator"]) == ("platform_constructs", "catalog", 30, 33)
    assert str(shop) not in json.dumps(report), "reports carry no absolute paths"
    assert not (shop / ".rkp" / "cache").exists(), "nothing was hydrated: both members were compiled here"


def test_the_f23_links(shop_links):
    r = shop_links
    links = {(x["rule"], x["from_member"], x["from_name"], x["to_member"], x["to_name"]) for x in r["links"]}
    assert links == {
        ("endpoint_path_unique", "web", "shop-web", "orders", "GET /api/orders/{order_id}"),
        ("endpoint_method_path", "web", "shop-web", "orders", "POST /api/orders"),
        ("same_table", "orders", "orders", "inputs", "orders"),
        ("same_column", "orders", "id", "inputs", "id"),
        ("same_column", "orders", "customer_id", "inputs", "customer_id"),
        ("same_column", "orders", "status", "inputs", "status"),
        ("same_column", "orders", "total", "inputs", "total"),
    }
    candidates = {(x["rule"], x["to_name"]) for x in r["candidates"]}
    assert candidates == {("endpoint_method_unstated", "GET /api/orders"), ("endpoint_method_unstated", "POST /api/orders"), ("config_key_name_only", "LOG_LEVEL")}
    # Both repositories read LOG_LEVEL from the environment; neither declares it for the
    # solution, so the shared name is a lead, not proof.
    config = next(x for x in r["candidates"] if x["rule"] == "config_key_name_only")
    assert (config["from_member"], config["to_member"], config["evidence_class"], config["predicate"]) == ("orders", "web", "E4", "ALIAS_OF")
    listing = next(x for x in r["candidates"] if x["to_name"] == "GET /api/orders")
    assert len(next(e for e in listing["evidence"] if e["member"] == "web")["evidence_ids"]) == 1, "only the call that states no method is a candidate"
    assert [(u["member"], u["kind"], u["reference"], u["reason"]) for u in r["unmatched"]] == [("web", "http_call", "/api/customers", "no_endpoint")]
    coverage = {(c["domain"], c["scope"]): (c["numerator"], c["denominator"]) for c in r["coverage"]}
    assert coverage == {("solution_http_calls", "web"): (2, 4), ("solution_data_refs", "orders"): (1, 1)}
    groups = {k: v for k, v in r["groups"].items() if k.startswith("inputs:")}
    assert len(groups) == 5 and all(len(v) == 2 for v in groups.values())


def test_a_copied_bundle_is_hydrated_into_the_solution_cache(shop, tmp_path):
    """A bundle delivered without its repository or cache: read from the committed files,
    checked against its digests, its HTTP calls unavailable until compiled here."""
    from repo_intel.solution import load_solution

    root = tmp_path / "clone"
    delivered = tmp_path / "delivered" / "web" / ".rkp"
    shutil.copytree(shop / "web" / ".rkp", delivered, ignore=shutil.ignore_patterns("cache"))
    (root / ".rkp").mkdir(parents=True)
    (root / ".rkp" / "solution.json").write_text(json.dumps({
        "schema": "rkp-solution/1", "name": "shop",
        "members": [{"id": "web", "bundle": "../delivered/web/.rkp", "role": "web"}, {"id": "orders", "path": os.path.relpath(shop / "service", root), "role": "service", "database": "shopdb"}],
    }))
    solution = load_solution(root)
    assert solution.problems == []
    web = solution.view.members["web"]
    assert web.exports is not None and web.info["detail"] == "architecture" and web.info["tier"] == "architecture"
    assert not any("its HTTP calls are not in the committed bundle" in w for w in solution.warnings)
    assert (root / ".rkp" / "cache" / "solution" / "web").is_dir() and not (delivered / "cache").exists()
    assert any(e["kind"] == "Component" for e in solution.view.entities(member="web"))
    r = resolve(solution.view)
    assert {x["rule"] for x in r.links} >= {"endpoint_path_unique", "endpoint_method_path"}, "the committed exports link the calls"
    assert [c for c in r.coverage if c["scope"] == "web"][0]["detail"]["available"] is True


def test_members_that_cannot_be_read_are_problems(shop, tmp_path):
    from repo_intel.solution import commands

    root = tmp_path / "broken"
    shutil.copytree(shop / "web" / ".rkp", root / "damaged" / ".rkp", ignore=shutil.ignore_patterns("cache"))
    unit = next(u for u in sorted((root / "damaged" / ".rkp" / "knowledge" / "units").glob("*.jsonl")) if '"kind":"Artifact"' in u.read_text())
    unit.write_text(unit.read_text().replace('"kind":"Artifact","name":"', '"kind":"Artifact","name":"edited-', 1))
    (root / "empty").mkdir()
    (root / ".rkp").mkdir(exist_ok=True)
    (root / ".rkp" / "solution.json").write_text(json.dumps({
        "schema": "rkp-solution/1", "name": "broken",
        "members": [{"id": "web", "path": "damaged", "role": "web"}, {"id": "gone", "path": "empty", "role": "batch"}],
        "inputs": [".rkp/inputs/catalog/*.json"],
    }))
    report = commands.check(root)
    assert not report["ok"]
    problems = " | ".join(report["problems"])
    assert "member web: the bundle does not match its manifest" in problems
    assert "member gone: no .rkp/ directory at empty" in problems
    assert "input .rkp/inputs/catalog/*.json matches no file" in problems
    assert [m["loaded"] for m in report["members"]] == [False, False]


def test_inputs_no_pack_reads_are_warnings(tmp_path):
    from repo_intel.solution import load_solution

    (tmp_path / ".rkp" / "inputs" / "notes").mkdir(parents=True)
    (tmp_path / ".rkp" / "inputs" / "notes" / "readme.txt").write_text("hello")
    (tmp_path / "web" / ".rkp").mkdir(parents=True)
    (tmp_path / ".rkp" / "solution.json").write_text(_manifest(inputs=[".rkp/inputs/notes/readme.txt"]))
    solution = load_solution(tmp_path)
    assert any("no platform pack reads this file" in w for w in solution.warnings)
    assert any("no bundle has been written at web" in p for p in solution.problems)
    artifact = next(solution.view.entities("Artifact", member="inputs"))
    assert artifact["natural_key"] == {"path": ".rkp/inputs/notes/readme.txt"} and artifact["attributes"]["parsed"] is False


# ------------------------------------------------------------------ the command line


def _run(capsys, *argv):
    from repo_intel import cli

    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_check_and_links(shop, capsys):
    code, out, _ = _run(capsys, "solution", "check", str(shop))
    assert code == 0 and out.startswith("ok: solution shop, 2 members")
    assert "inputs: 2 files, 28 entities" in out and "platform_constructs catalog: 30/33" in out
    code, out, _ = _run(capsys, "solution", "check", str(shop), "--json")
    [line] = out.splitlines()
    event = json.loads(line)
    assert code == 0 and event["event"] == "result" and event["ok"] is True and event["solution"] == "shop"
    code, out, _ = _run(capsys, "solution", "links", str(shop))
    assert code == 0 and out.startswith("solution shop: 7 links, 3 candidates, 1 unmatched")
    assert "endpoint_path_unique: web:Component shop-web -CONSUMES-> orders:Endpoint GET /api/orders/{order_id}" in out
    assert "web: http_call /api/customers (no_endpoint)" in out
    code, out, _ = _run(capsys, "solution", "links", str(shop), "--json")
    event = json.loads(out)
    assert code == 0 and event["event"] == "result" and len(event["links"]) == 7 and len(event["candidates"]) == 3


def test_cli_reports_an_invalid_manifest(tmp_path, capsys):
    (tmp_path / ".rkp").mkdir()
    (tmp_path / ".rkp" / "solution.json").write_text(_manifest(schema="rkp-solution/0"))
    code, out, err = _run(capsys, "solution", "check", str(tmp_path))
    assert code == 1 and out == "" and "schema must be 'rkp-solution/1'" in err
    code, out, _ = _run(capsys, "solution", "links", str(tmp_path), "--json")
    event = json.loads(out)
    assert code == 1 and event["event"] == "error" and event["kind"] == "SolutionError"
    with pytest.raises(SystemExit) as exit_:
        _run(capsys, "solution")
    assert exit_.value.code == 2
