"""Talend project items (repo_intel/platforms/talend): job designs (`.item`) read into Jobs,
component Tasks in connection order, the files and tables they read and write, transform
shapes per job and behaviour for tMap and for jobs that run jobs; `.properties` read into
job versions and routines."""

import hashlib
import json
import re
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import validate_unit
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.compile.transforms import validate_shape
from repo_intel.platforms import packs, validate_pack
from repo_intel.platforms.talend import PACK, item, javaexpr, source, vocabulary
from repo_intel.platforms.talend.source import TalendItem, TalendProperties

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "F33-talend-job"
CUSTOMERS = "DWH/process/Customers/LoadCustomers_0.1.item"
ORDERS = "DWH/process/Orders/LoadOrders_0.1.item"
NIGHTLY = "DWH/process/Nightly_0.1.item"


def fixture(rel: str, layer: str = "base") -> bytes:
    return (FIXTURE / layer / rel).read_bytes()


def run(data: bytes, path: str = "DWH/process/Sample_0.1.item"):
    extractor = TalendProperties() if path.endswith(".properties") else TalendItem()
    return extractor.extract(path, data, hashlib.sha256(data).hexdigest())


def ref(x) -> tuple:
    return x if isinstance(x, str) else (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def key(entity_kind: str, **nk) -> tuple:
    return (entity_kind, json.dumps(nk, sort_keys=True))


def job(name: str) -> tuple:
    return key("Job", framework="talend", name=name)


def task(j: str, name: str) -> tuple:
    return key("Task", framework="talend", job=j, name=name)


def table(name: str) -> tuple:
    return key("Table", datastore="default", name=name)


def dataset(name: str) -> tuple:
    return key("Dataset", name=name)


def entities(out) -> dict[tuple, dict]:
    found: dict[tuple, dict] = {}
    for o in out.observations:
        if o.kind == "entity":
            found.setdefault(ref(o.payload), {}).update(o.payload["attributes"])
    return found


def relations(out) -> set[tuple]:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])) for o in out.observations if o.kind == "relation"}


def qualifiers(out, subject, predicate, obj) -> dict:
    for o in out.observations:
        p = o.payload
        if o.kind == "relation" and (ref(p["subject"]), p["predicate"], ref(p["object"])) == (subject, predicate, obj):
            return p.get("qualifiers") or {}
    raise AssertionError("no such relation")


def constructs(out) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = {}
    for o in out.observations:
        if o.kind == "construct":
            k = (o.payload["construct"], o.payload["modelled"])
            counts[k] = counts.get(k, 0) + 1
    return counts


def units(out, kind: str) -> list[dict]:
    return [o.payload for o in out.observations if o.kind == kind]


def transforms(out, job_name: str = "Sample") -> list[dict]:
    found = [u for u in units(out, "transform") if u["owner"]["natural_key"]["name"] == job_name]
    assert len(found) == 1
    return found[0]["transforms"]


def messages(out) -> list[str]:
    return [d.message for d in out.diagnostics]


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def check_all_valid(out) -> None:
    for u in units(out, "behavior"):
        assert validate_unit(u) == [], u
    for u in units(out, "transform"):
        assert validate_shape(u) == [], u
    for o in out.observations:
        p = o.payload
        if o.kind == "relation" and isinstance(p["subject"], dict) and isinstance(p["object"], dict):
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p


# ---------------------------------------------------------------------- writing small items


def node(component: str, unique: str, params: dict | None = None, tables: dict | None = None, body: str = "") -> str:
    ps = [f'<elementParameter field="TEXT" name="UNIQUE_NAME" value="{unique}"/>']
    for k, v in (params or {}).items():
        ps.append(f"<elementParameter field=\"TEXT\" name=\"{k}\" value={quoteattr(v)}/>")
    for k, rows in (tables or {}).items():
        values = "".join(f"<elementValue elementRef=\"{rk}\" value={quoteattr(rv)}/>" for row in rows for rk, rv in row.items())
        ps.append(f'<elementParameter field="TABLE" name="{k}">{values}</elementParameter>')
    return f'<node componentName="{component}" componentVersion="0.1" posX="0" posY="0">{"".join(ps)}{body}</node>'


def conn(src: str, dst: str, label: str, connector: str = "FLOW", style: str = "0", condition: str | None = None) -> str:
    extra = f"<elementParameter field=\"MEMO_JAVA\" name=\"CONDITION\" value={quoteattr(condition)}/>" if condition else ""
    return f'<connection connectorName="{connector}" label="{label}" lineStyle="{style}" source="{src}" target="{dst}">{extra}</connection>'


def process(*parts: str, job_type: str = "Standard", context: str = "") -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<talendfile:ProcessType xmlns:xmi="http://www.omg.org/XMI" '
            'xmlns:talendfile="platform:/resource/org.talend.model/model/TalendFile.xsd" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            f'jobType="{job_type}">{context}' + "".join(parts) + "</talendfile:ProcessType>\n").encode()


def src(unique: str = "tFileInputDelimited_1", path: str = '"/in/a.csv"') -> str:
    return node("tFileInputDelimited", unique, {"FILENAME": path})


def tgt(unique: str = "tFileOutputDelimited_1", path: str = '"/out/b.csv"') -> str:
    return node("tFileOutputDelimited", unique, {"FILENAME": path})


def chain(*middle: str) -> list[str]:
    """A file source, the given components (one input, one FLOW output each), a file target."""
    names = ["tFileInputDelimited_1"] + [re.search(r'name="UNIQUE_NAME" value="([^"]+)"', m).group(1) for m in middle] + ["tFileOutputDelimited_1"]
    return [src(), *middle, tgt()] + [conn(a, b, f"row{i}") for i, (a, b) in enumerate(zip(names, names[1:]), start=1)]


# ---------------------------------------------------------------------- registration


def test_registered_as_the_talend_pack():
    assert packs()["talend"] is PACK
    assert validate_pack(PACK) == []
    assert {e.id for e in PACK.extractors} == {"talend.item", "talend.properties"}
    assert {"tal:StandardJob", "tal:BigDataJob", "tal:Joblet", "tal:Component", "tal:Routine"} <= {t.name for t in PACK.terms}


def test_properties_files_are_matched_only_in_talend_folders():
    assert TalendProperties().matches("DWH/process/a/X_0.1.properties")
    assert TalendProperties().matches("DWH/code/routines/R_0.1.properties")
    assert not TalendProperties().matches("src/main/resources/application.properties")
    assert TalendItem().matches("DWH/process/X_0.1.item") and not TalendItem().matches("x.xml")
    out = run(b"server.port=8080\ndb.password=secret\n", "DWH/process/app.properties")
    assert not out.parsed and not out.observations and not out.diagnostics


def test_every_construct_and_term_the_reader_names_is_listed():
    ids = {c.id for c in PACK.constructs}
    terms = {t.name for t in PACK.terms}
    pkg = Path(source.__file__).parent
    used = set()
    for name in ("item.py", "source.py", "javaexpr.py"):
        used |= set(re.findall(r'"(tal:[A-Za-z_][A-Za-z0-9_.]*)"', (pkg / name).read_text()))
    assert used
    assert not {u for u in used if u not in ids and u not in terms}
    for name, *_ in vocabulary._COMPONENTS:
        assert f"tal:component.{name}" in ids
    assert vocabulary.component_kind("TMAP")[0] == "tMap" and vocabulary.component_kind("tOracleOutput")[2] == "oracle"
    assert vocabulary.component_kind("tNoSuchThing") is None


def test_job_identity_from_the_file_name():
    assert item.job_identity("DWH/process/Customers/LoadCustomers_0.1.item") == ("LoadCustomers", "0.1")
    assert item.job_identity("DWH/process/Load_Customers_1.12.item") == ("Load_Customers", "1.12")
    assert item.job_identity("x/Plain.item") == ("Plain", None)
    assert item.folder("DWH/process/A/B/X_0.1.item") == "A/B" and item.folder("DWH/process/X_0.1.item") is None


# ---------------------------------------------------------------------- the fixture


def test_job_components_connections_and_data():
    out = run(fixture(CUSTOMERS), CUSTOMERS)
    assert out.parsed
    ents, rels = entities(out), relations(out)
    j = ents[job("LoadCustomers")]
    assert j["platform_kind"] == "tal:StandardJob" and j["version"] == "0.1" and j["folder"] == "Customers"
    assert j["context_parameters"] == ["dw_password", "reject_dir"] and j["contexts"] == ["Default"]
    for n in ("tPrejob_1", "tOracleConnection_1", "tFileInputDelimited_1", "tOracleInput_1", "tMap_1", "tSortRow_1", "tUniqRow_1", "tOracleOutput_1",
              "tFileOutputDelimited_1", "tPostjob_1", "tOracleClose_1"):
        assert (job("LoadCustomers"), "CONTAINS", task("LoadCustomers", n)) in rels
    assert ents[task("LoadCustomers", "tMap_1")]["label"] == "clean_customers"
    assert ents[task("LoadCustomers", "tOracleInput_1")]["database"] == "oracle"
    assert qualifiers(out, task("LoadCustomers", "tOracleOutput_1"), "DEPENDS_ON", task("LoadCustomers", "tUniqRow_1")) == {"via": "flow", "connector": "UNIQUE", "link": "row4"}
    assert qualifiers(out, task("LoadCustomers", "tOracleConnection_1"), "DEPENDS_ON", task("LoadCustomers", "tPrejob_1"))["via"] == "trigger"
    assert (task("LoadCustomers", "tFileInputDelimited_1"), "READS_FROM", dataset("/data/landing/customers.csv")) in rels
    assert (task("LoadCustomers", "tOracleInput_1"), "READS_FROM", table("dw.country")) in rels
    assert (task("LoadCustomers", "tOracleOutput_1"), "WRITES_TO", table("dw.customers")) in rels
    check_all_valid(out)


def test_tmap_shapes_join_filter_and_projections():
    out = run(fixture(CUSTOMERS), CUSTOMERS)
    ts = transforms(out, "LoadCustomers")
    join = next(t for t in ts if t["op"] == "join")
    assert join["primitive"] == "join" and join["join"] == {"type": "left", "on": [["COUNTRY_CODE", "COUNTRY_CODE"]]}
    splits = [t for t in ts if t["op"] == "filter" and t.get("primitive") == "split_by_condition"]
    assert len(splits) == 2 and all(s["inputs"] == [join["id"]] for s in splits)
    prims = {t.get("primitive") for t in ts}
    assert {"trim_strings", "normalize_case", "apply_defaults", "parse_timestamp", "conditional_flag", "dedupe_by_key", "merge_into"} <= prims
    assert next(t for t in ts if t["op"] == "sort")["order"] == [{"column": "CUST_ID", "desc": False}]
    assert next(t for t in ts if t["op"] == "deduplicate")["keys"] == ["CUST_ID"]
    rejects = next(t for t in ts if t["op"] == "publish" and t.get("dynamic"))
    assert rejects["output"] == "{context.reject_dir}/customers_rejects.csv"


def test_tmap_behaviour():
    data = fixture(CUSTOMERS)
    out = run(data, CUSTOMERS)
    (unit,) = [u for u in units(out, "behavior") if u["owner"]["kind"] == "Task"]
    assert unit["owner"]["natural_key"]["name"] == "tMap_1" and unit["kind"] == "flow" and unit["language"] == "talend"
    loop = unit["body"][0]
    assert loop["op"] == "loop" and loop["kind"] == "implicit" and loop["over"] == {"e": "var", "scope": "link", "name": "row1"}
    assert loop["body"][0]["target"] == {"e": "var", "scope": "variable", "name": "isVip"}
    guard = next(s for s in loop["body"] if s["op"] == "if")
    assert {s["name"] for s in guard["then"] if s["op"] == "output"} == {"out_valid"}
    assert any(s["op"] == "output" and s["name"] == "out_reject" for s in loop["body"])
    calls = {n["callee"].get("name") for n in walk(unit["body"]) if n.get("e") == "call"}
    assert {"StringHandling.TRIM", "toUpperCase", "TalendDate.parseDate", "equals"} <= calls
    a, b = loop["at"]
    assert b"tMap_1" in data[a:b]


def test_a_name_built_from_a_context_variable_stays_dynamic():
    out = run(fixture(CUSTOMERS), CUSTOMERS)
    assert not any(k[0] == "Dataset" and "rejects" in k[1] for k in entities(out))
    assert constructs(out)[("tal:dynamic_name", False)] == 1
    assert messages(out) == ["component tFileOutputDelimited_1 of LoadCustomers writes a file named by context variables or code: not resolved"]


def test_a_context_variable_inside_a_literal_is_not_a_dynamic_table():
    out = run(fixture(ORDERS), ORDERS)
    assert (task("LoadOrders", "tPostgresqlInput_1"), "READS_FROM", table("sales.orders")) in relations(out)
    assert not messages(out)
    agg = next(t for t in transforms(out, "LoadOrders") if t["op"] == "aggregate")
    assert agg["keys"] == ["cust_id"]


def test_values_hosts_users_passwords_and_addresses_are_never_copied():
    probes = ("FIXTURE-NOT-A-REAL-SECRET", "fixture-not-a-real-dir", "dwhost.fixture.invalid", "fixture_etl_user", "salesdb.fixture.invalid",
              "dwh-support@fixture.invalid", "etl@fixture.invalid", "mail.fixture.invalid", "2024-01-01", "_fixture_author", "2024-03-04")
    for rel in (CUSTOMERS, ORDERS, NIGHTLY, "DWH/process/Customers/LoadCustomers_0.1.properties"):
        out = run(fixture(rel), rel)
        blob = json.dumps([o.payload for o in out.observations], default=str) + repr(messages(out))
        for probe in probes:
            assert probe not in blob, (rel, probe)
    assert b"FIXTURE-NOT-A-REAL-SECRET" in fixture(CUSTOMERS)


def test_a_job_that_runs_jobs():
    out = run(fixture(NIGHTLY), NIGHTLY)
    rels = relations(out)
    assert (task("Nightly", "tRunJob_1"), "RUNS", job("LoadCustomers")) in rels
    assert (task("Nightly", "tRunJob_2"), "RUNS", job("LoadOrders")) in rels
    assert job("LoadCustomers") not in entities(out)
    assert qualifiers(out, task("Nightly", "tRunJob_2"), "DEPENDS_ON", task("Nightly", "tRunJob_1")) == {"via": "trigger", "connector": "RUN_IF", "link": "If (customers ok)"}
    (unit,) = units(out, "behavior")
    assert unit["owner"]["kind"] == "Job" and unit["kind"] == "flow"
    body = unit["body"]
    assert body[0]["op"] == "call" and body[0]["call"]["callee"] == {"e": "name", "name": "LoadCustomers", "kind": "job"}
    assert body[1]["op"] == "if" and body[1]["then"][0]["call"]["callee"]["name"] == "LoadOrders"
    cond = body[1]["cond"]
    assert cond["op"] == "==" and cond["left"]["callee"] == {"e": "name", "name": "(Integer)", "kind": "cast"}
    assert body[-1] == {"op": "throw", "at": body[-1]["at"]}
    assert constructs(out)[("tal:run_if_condition", True)] == 2
    check_all_valid(out)


def test_properties_declare_the_job_and_routines():
    out = run(fixture("DWH/process/Customers/LoadCustomers_0.1.properties"), "DWH/process/Customers/LoadCustomers_0.1.properties")
    attrs = entities(out)[job("LoadCustomers")]
    assert attrs["platform_kind"] == "tal:StandardJob" and attrs["version"] == "0.1" and attrs["status"] == "PROD"
    assert attrs["purpose"] == "Load the customer dimension" and attrs["folder"] == "Customers"
    out = run(fixture("DWH/code/routines/DateUtils_0.1.properties"), "DWH/code/routines/DateUtils_0.1.properties")
    assert entities(out)[key("Program", platform="talend", name="DateUtils")]["platform_kind"] == "tal:Routine"
    out = run(fixture("DWH/code/routines/DateUtils_0.1.item"), "DWH/code/routines/DateUtils_0.1.item")
    assert constructs(out) == {("tal:code_item", False): 1} and not entities(out)


def test_other_properties_items_are_counted():
    def props(kind: str) -> bytes:
        return (f'<?xml version="1.0"?><xmi:XMI xmlns:xmi="http://www.omg.org/XMI" xmlns:TalendProperties="http://www.talend.org/properties">'
                f'<TalendProperties:Property label="x" version="0.1"/><TalendProperties:{kind}/></xmi:XMI>').encode()

    assert ("tal:context_item", False) in constructs(run(props("ContextItem"), "DWH/context/x_0.1.properties"))
    assert ("tal:metadata_item", False) in constructs(run(props("DatabaseConnectionItem"), "DWH/metadata/connections/x_0.1.properties"))
    assert ("tal:item_other", False) in constructs(run(props("BusinessProcessItem"), "DWH/businessProcess/x_0.1.properties"))
    joblet = run(props("JobletProcessItem").replace(b'label="x"', b'label="Audit"'), "DWH/joblets/Audit_0.1.properties")
    assert entities(joblet)[job("Audit")]["platform_kind"] == "tal:Joblet"


def test_fixture_layers():
    out = run(fixture(ORDERS, "commits/01-filter-orders"), ORDERS)
    flt = next(t for t in transforms(out, "LoadOrders") if t["op"] == "filter" and isinstance(t.get("predicate"), dict) and t["predicate"].get("e"))
    assert flt["predicate"]["op"] == "!=" and flt["predicate"]["right"] == {"e": "lit", "value": "CANCELLED", "type": "string"}
    assert (task("LoadOrders", "tAggregateRow_1"), "DEPENDS_ON", task("LoadOrders", "tFilterRow_1")) in relations(out)
    rel = "DWH/process/Products/LoadProducts_0.1.item"
    out = run(fixture(rel, "commits/02-product-job"), rel)
    assert (task("LoadProducts", "tMSSqlOutput_1"), "WRITES_TO", table("dbo.products")) in relations(out)
    ts = transforms(out, "LoadProducts")
    assert any(t["op"] == "platform" and t["construct"] == "tal:component.tJavaRow" for t in ts)
    assert constructs(out)[("tal:component.tJavaRow", False)] == 1
    check_all_valid(out)


def test_output_is_deterministic():
    for rel in (CUSTOMERS, ORDERS, NIGHTLY):
        dump = lambda out: [json.dumps([o.kind, o.payload, o.evidence.span.start_byte, o.evidence.span.end_byte], sort_keys=True, default=str) for o in out.observations]
        a, b = run(fixture(rel), rel), run(fixture(rel), rel)
        assert dump(a) == dump(b) and messages(a) == messages(b)


def test_spans_point_into_the_file():
    data = fixture(CUSTOMERS)
    out = run(data, CUSTOMERS)
    for o in out.observations:
        s = o.evidence.span
        assert 0 <= s.start_byte <= s.end_byte <= len(data)
    src_entity = next(o for o in out.observations if o.kind == "entity" and o.payload["natural_key"] == {"name": "/data/landing/customers.csv"})
    assert b"/data/landing/customers.csv" in data[src_entity.evidence.span.start_byte:src_entity.evidence.span.end_byte]


# ---------------------------------------------------------------------- refusals


def test_not_talend_and_malformed_items_are_refused():
    out = run(b"<root/>", "x/Y_0.1.item")
    assert not out.parsed and not out.diagnostics
    out = run(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "boom">]><talendfile:ProcessType xmlns:talendfile="talend"/>', "x/Y_0.1.item")
    assert not out.parsed and messages(out)[0].startswith("not read:")
    out = run(b'<talendfile:ProcessType xmlns:talendfile="talend"><node', "x/Y_0.1.item")
    assert not out.parsed


def test_a_file_name_that_is_not_a_label():
    out = run(process(*chain()), "DWH/process/bad name!_0.1.item")
    assert not entities(out) and constructs(out) == {("tal:job", False): 1}


# ---------------------------------------------------------------------- component families


def test_filter_row_simple_and_advanced():
    simple = node("tFilterRow", "tFilterRow_1", {"LOGICAL_OP": "||"},
                  {"CONDITIONS": [{"INPUT_COLUMN": "A", "FUNCTION": "", "OPERATOR": ">", "RVALUE": "1"}, {"INPUT_COLUMN": "B", "FUNCTION": "", "OPERATOR": "==", "RVALUE": '"x"'}]})
    out = run(process(*chain(simple)))
    flt = next(t for t in transforms(out) if t["op"] == "filter")
    assert flt["predicate"]["op"] == "or" and flt["predicate"]["left"]["left"] == {"e": "member", "object": {"e": "var", "scope": "link", "name": "input_row"}, "name": "A"}
    advanced = node("tFilterRow", "tFilterRow_1", {"USE_ADVANCED": "true", "ADVANCED_COND": "input_row.A != null && input_row.A.startsWith(\"Z\")"})
    parts = [src(), advanced, tgt(), tgt("tFileOutputDelimited_2", '"/out/rejects.csv"'),
             conn("tFileInputDelimited_1", "tFilterRow_1", "row1"), conn("tFilterRow_1", "tFileOutputDelimited_1", "kept", "FILTER"),
             conn("tFilterRow_1", "tFileOutputDelimited_2", "dropped", "REJECT")]
    out = run(process(*parts))
    ts = transforms(out)
    filters = [t for t in ts if t["op"] == "filter"]
    assert len(filters) == 2 and all(f["primitive"] == "split_by_condition" for f in filters)
    assert filters[1]["predicate"]["op"] == "not"
    assert {t["output"] for t in ts if t["op"] == "publish"} == {"/out/b.csv", "/out/rejects.csv"}
    check_all_valid(out)


def test_join_union_convert_filter_columns_and_replicate():
    join = node("tJoin", "tJoin_1", {"USE_INNER_JOIN": "true"}, {"JOIN_KEY": [{"INPUT_COLUMN": "ID", "LOOKUP_COLUMN": "REF_ID"}]})
    parts = [src(), src("tFileInputDelimited_2", '"/in/ref.csv"'), join, tgt(),
             conn("tFileInputDelimited_1", "tJoin_1", "main"), conn("tFileInputDelimited_2", "tJoin_1", "lookup", style="8"),
             conn("tJoin_1", "tFileOutputDelimited_1", "joined")]
    out = run(process(*parts))
    j = next(t for t in transforms(out) if t["op"] == "join")
    assert j["join"] == {"type": "inner", "on": [["ID", "REF_ID"]]} and j["primitive"] == "join"
    unite = node("tUnite", "tUnite_1")
    parts = [src(), src("tFileInputDelimited_2", '"/in/more.csv"'), unite, tgt(),
             conn("tFileInputDelimited_1", "tUnite_1", "a"), conn("tFileInputDelimited_2", "tUnite_1", "b"), conn("tUnite_1", "tFileOutputDelimited_1", "c")]
    assert next(t for t in transforms(run(process(*parts))) if t["op"] == "union")["candidates"] == ["union_by_name"]
    ts = transforms(run(process(*chain(node("tConvertType", "tConvertType_1"), node("tFilterColumns", "tFilterColumns_1"), node("tReplicate", "tReplicate_1")))))
    assert [(t["op"], t.get("primitive"), t.get("candidates")) for t in ts][1:3] == [("project", None, ["safe_cast"]), ("project", "select_columns", None)]


def test_database_components():
    db_in = node("tDBInput", "tDBInput_1", {"TYPE": "MSSQL", "QUERY": '"SELECT a FROM dbo.src"'})
    db_out = node("tDB2Output", "tDB2Output_1", {"TABLE": '"T"', "SCHEMA_DB": '"S"', "DATA_ACTION": "DELETE", "PASS": '"pw-not-real-77"', "USER": '"user-not-real-77"'})
    parts = [db_in, db_out, conn("tDBInput_1", "tDB2Output_1", "row1")]
    out = run(process(*parts))
    ents, rels = entities(out), relations(out)
    assert ents[task("Sample", "tDBInput_1")]["database"] == "mssql"
    assert (task("Sample", "tDBInput_1"), "READS_FROM", table("dbo.src")) in rels
    assert (task("Sample", "tDB2Output_1"), "WRITES_TO", table("s.t")) in rels
    assert next(t for t in transforms(out) if t["op"] == "publish")["primitive"] == "apply_deletes"
    assert "not-real-77" not in json.dumps([o.payload for o in out.observations], default=str)
    row = node("tOracleRow", "tOracleRow_1", {"QUERY": '"UPDATE dw.flags SET done = 1 WHERE id IN (SELECT id FROM dw.batch)"'})
    out = run(process(row))
    rels = relations(out)
    assert (task("Sample", "tOracleRow_1"), "WRITES_TO", table("dw.flags")) in rels
    assert (task("Sample", "tOracleRow_1"), "READS_FROM", table("dw.batch")) in rels
    dyn = node("tOracleOutput", "tOracleOutput_1", {"TABLE": 'context.schema + ".T"'})
    out = run(process(src(), dyn, conn("tFileInputDelimited_1", "tOracleOutput_1", "row1")))
    assert not any(k[0] == "Table" for k in entities(out))
    assert any("writes a table named by context variables or code" in m for m in messages(out))
    check_all_valid(out)


def test_inactive_unknown_and_java_components():
    off = node("tSortRow", "tSortRow_1", {"ACTIVATE": "false"})
    custom = node("tMyCompanyStage", "tMyCompanyStage_1")
    java = node("tJavaFlex", "tJavaFlex_1", {"CODE_MAIN": 'output_row.x = "secret-code-not-copied";'})
    out = run(process(*chain(off, custom, java)))
    ents, counts = entities(out), constructs(out)
    assert ents[task("Sample", "tSortRow_1")]["active"] is False
    assert counts[("tal:inactive_component", False)] == 1 and counts[("tal:component.tSortRow", False)] == 1
    assert counts[("tal:component.unknown", False)] == 1 and ents[task("Sample", "tMyCompanyStage_1")]["family"] == "unknown"
    assert counts[("tal:component.tJavaFlex", False)] == 1
    assert "secret-code-not-copied" not in json.dumps([o.payload for o in out.observations])
    plats = [t["construct"] for t in transforms(out) if t["op"] == "platform"]
    assert plats == ["tal:component.tSortRow", "tal:component.unknown", "tal:component.tJavaFlex"]


def test_a_run_job_chosen_at_run_time_is_not_resolved():
    out = run(process(node("tRunJob", "tRunJob_1", {"USE_DYNAMIC_JOB": "true", "PROCESS": "LoadX"})))
    assert not any(p == "RUNS" for _, p, _ in relations(out))
    assert "component tRunJob_1 of Sample runs a job chosen at run time: not resolved" in messages(out)


def test_job_types_and_duplicates():
    out = run(process(*chain(), job_type="Big Data Batch"))
    assert entities(out)[job("Sample")]["platform_kind"] == "tal:BigDataJob"
    out = run(process(*chain()), "DWH/joblets/Sample_0.1.item")
    assert entities(out)[job("Sample")]["platform_kind"] == "tal:Joblet"
    out = run(process(src(), src()))
    assert task("Sample", "tFileInputDelimited_1~2") in entities(out)


def test_tmap_lookup_modes_and_parse_failures():
    data = ('<nodeData xsi:type="MapperData">'
            '<outputTables name="o"><mapperTableEntries name="X" expression="row1.A +* 2"/><mapperTableEntries name="Y" expression="row2.B"/></outputTables>'
            '<inputTables name="row1"><mapperTableEntries name="A"/></inputTables>'
            '<inputTables name="row2" matchingMode="ALL_ROWS"><mapperTableEntries name="B"/></inputTables></nodeData>')
    parts = [src(), src("tFileInputDelimited_2", '"/in/ref.csv"'), node("tMap", "tMap_1", body=data), tgt(),
             conn("tFileInputDelimited_1", "tMap_1", "row1"), conn("tFileInputDelimited_2", "tMap_1", "row2", style="8"), conn("tMap_1", "tFileOutputDelimited_1", "o")]
    out = run(process(*parts))
    j = next(t for t in transforms(out) if t["op"] == "join")
    assert j["join"] == {"type": "cross", "on": []}
    assert constructs(out)[("tal:expression_unparsed", False)] == 1
    assert constructs(out)[("tal:component.tMap", False)] == 1
    check_all_valid(out)


# ---------------------------------------------------------------------- Java expressions


def test_java_expression_subset():
    scope = javaexpr.Scope(links={"row1"})
    e = javaexpr.parse('row1.A == null ? "n/a" : StringHandling.UPCASE(row1.A.trim())', scope)
    assert e["e"] == "cond" and e["else"]["callee"] == {"e": "name", "name": "StringHandling.UPCASE", "kind": "function"}
    assert e["else"]["args"][0]["callee"] == {"e": "member", "object": {"e": "member", "object": {"e": "var", "scope": "link", "name": "row1"}, "name": "A"}, "name": "trim"}
    e = javaexpr.parse("!(context.limit > 10L) || Var.x", scope)
    assert e["op"] == "or" and e["left"]["operand"]["left"] == {"e": "var", "scope": "context", "name": "limit"}
    assert e["left"]["operand"]["right"] == {"e": "lit", "value": 10, "type": "integer"} and e["right"] == {"e": "var", "scope": "variable", "name": "x"}
    assert javaexpr.parse("new java.math.BigDecimal(\"1.5\")")["type"] == "java.math.BigDecimal"
    assert javaexpr.parse("1.5d")["type"] == "number" and javaexpr.parse("'c'")["value"] == "c"
    assert javaexpr.link_columns(javaexpr.parse("row1.A + row1.B * row1.A", scope)) == ["A", "B"]
    for bad in ("a = 1", "x -> x", "a & b", "a instanceof String", "", "(1"):
        with pytest.raises(javaexpr.ExprError):
            javaexpr.parse(bad, scope)


def test_java_string_values():
    sv = javaexpr.string_value
    assert sv('"/data/in.csv"') == ("/data/in.csv", False)
    assert sv('context.dir + "/x_" + TalendDate.getDate("yyyyMMdd") + ".csv"') == ("{context.dir}/x_{expr}.csv", True)
    assert sv('"SELECT * FROM " + context.schema + ".T"', sql=True) == ("SELECT * FROM #context.schema#.T", True)
    assert sv("context.path") == ("{context.path}", True)
    assert sv("true") == (None, False) and sv("") == (None, False) and sv(None) == (None, False)
    assert sv('"a\\"b"') == ('a"b', False)


def test_two_versions_of_a_job_keep_their_components_apart():
    first = run(process(*chain()), "DWH/process/Sample_0.1.item")
    second = run(process(*chain()), "DWH/process/Sample_0.2.item")
    tasks = [{k for k in entities(out) if k[0] == "Task"} for out in (first, second)]
    assert task("Sample", "tFileInputDelimited_1") in tasks[0] and task("Sample", "tFileInputDelimited_1@0.2") in tasks[1]
    assert tasks[0] and not tasks[0] & tasks[1], "one version's components never merge with another's"
    lineage = [{r for r in relations(out) if r[0][0] == "Task" and r[1] in ("READS_FROM", "WRITES_TO")} for out in (first, second)]
    assert lineage[0] and lineage[1] and not lineage[0] & lineage[1]
    assert {r[2] for r in lineage[0]} == {r[2] for r in lineage[1]}, "both versions read and write the same data"
    assert job("Sample") in entities(first) and job("Sample") in entities(second), "both versions are the one Job"
    assert (job("Sample"), "CONTAINS", task("Sample", "tFileOutputDelimited_1@0.2")) in relations(second)
    assert not messages(first)
    assert messages(second) == ["DWH/process/Sample_0.2.item is version 0.2 of job Sample: other versions may be in the repository, "
                                "so its components are named <unique name>@0.2"]
    check_all_valid(second)
