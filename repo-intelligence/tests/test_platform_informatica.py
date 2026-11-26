"""The Informatica pack (repo_intel.platforms.informatica): PowerCenter XML exports read into
Jobs, Tasks, Programs, ConfigKeys, transform shapes and behaviour units, and parameter files
read by name only."""

import hashlib
import json
import re
from pathlib import Path

from repo_intel import extractors  # noqa: F401  (loads the extractors before the packs)
from repo_intel.compile.behavior import validate_unit
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.compile.transforms import validate_shape
from repo_intel.ingest import secrets
from repo_intel.platforms import packs, validate_pack
from repo_intel.platforms.informatica import PACK
from repo_intel.platforms.informatica.source import InformaticaExport, InformaticaParameters

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "F31-informatica-export"
PKG = Path(__file__).resolve().parents[1] / "repo_intel" / "platforms" / "informatica"
SALES = "informatica/exports/sales_folder.xml"
RETURNS = "informatica/exports/returns_folder.xml"
PARAMS = "informatica/params/wf_daily_sales.par"
W = "SALES/wf_daily_sales"
NEVER = ("fixture-not-a-real-password", "files.example.invalid", "etl-oncall@", "/opt/infa/params", "sqlplus", "IS_ETL_PROD", "1900-01-01", "/data/in",
         "EMEA", "ORA_DW", "2026-01-05", "etl_admin")


def admitted(data: bytes) -> bytes:
    return secrets.mask_for_extraction(data, secrets.scan(data))


def layer_bytes(path: str, layer: str = "base") -> bytes:
    return (FIXTURE / layer / path).read_bytes()


def run(extractor, path: str, data: bytes):
    data = admitted(data)
    return extractor.extract(path, data, hashlib.sha256(data).hexdigest())


def export(path: str = SALES, layer: str = "base", data: bytes | None = None):
    return run(InformaticaExport(), path, data if data is not None else layer_bytes(path, layer))


def returns():
    return export(RETURNS, "commits/01-returns-mapping")


def parameters(data: bytes | None = None, path: str = PARAMS):
    return run(InformaticaParameters(), path, data if data is not None else layer_bytes(PARAMS))


def ref(x) -> tuple:
    return (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def key(entity_kind: str, **nk) -> tuple:
    return (entity_kind, json.dumps(nk, sort_keys=True))


def job(name: str = W) -> tuple:
    return key("Job", framework="informatica", name=name)


def task(name: str, job_name: str = W) -> tuple:
    return key("Task", framework="informatica", job=job_name, name=name)


def prog(name: str) -> tuple:
    return key("Program", platform="informatica", name=name)


def table(name: str) -> tuple:
    return key("Table", datastore="default", name=name)


def cfg(name: str) -> tuple:
    return key("ConfigKey", name=name)


def entities(out) -> dict[tuple, dict]:
    found: dict[tuple, dict] = {}
    for o in out.observations:
        if o.kind == "entity":
            found.setdefault(ref(o.payload), {}).update(o.payload["attributes"])
    return found


def relations(out) -> dict[tuple, dict]:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])): o.payload["qualifiers"] for o in out.observations if o.kind == "relation"}


def units(out, kind: str) -> list[dict]:
    return [o.payload for o in out.observations if o.kind == kind]


def unit_of(out, kind: str, name: str) -> dict:
    return next(u for u in units(out, kind) if u["owner"]["natural_key"]["name"] == name)


def constructs(out) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = {}
    for o in out.observations:
        if o.kind == "construct":
            k = (o.payload["construct"], o.payload["modelled"])
            counts[k] = counts.get(k, 0) + 1
    return counts


def messages(out) -> list[str]:
    return [d.message for d in out.diagnostics]


def blob(out) -> str:
    return json.dumps([o.payload for o in out.observations], default=str) + json.dumps(messages(out))


def check_contract(out, data: bytes) -> None:
    for u in units(out, "behavior"):
        assert validate_unit(u) == [], u
    for u in units(out, "transform"):
        assert validate_shape(u) == [], u
    for o in out.observations:
        s = o.evidence.span
        assert 0 <= s.start_byte <= s.end_byte <= len(data), o.payload
        if o.kind == "relation":
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p


def edited(old: bytes, new: bytes, path: str = SALES, layer: str = "base") -> bytes:
    data = layer_bytes(path, layer)
    assert old in data, old
    return data.replace(old, new, 1)


def steps(shape: dict, op: str) -> list[dict]:
    return [t for t in shape["transforms"] if t["op"] == op]


# ---------------------------------------------------------------------- registration


def test_registered_and_valid():
    assert packs()["informatica"] is PACK
    assert validate_pack(PACK) == []
    assert PACK.prefix == "infa" and {e.id for e in PACK.extractors} == {"informatica.export", "informatica.params"}
    assert InformaticaExport().matches("a/B.XML") and not InformaticaExport().matches("a/b.par")
    assert all(InformaticaParameters().matches(f"p/x.{ext}") for ext in ("par", "prm", "parm"))


def test_every_construct_the_pack_names_is_listed():
    ids = {c.id for c in PACK.constructs}
    used = set()
    for f in PKG.glob("*.py"):
        used |= set(re.findall(r'"(infa:[a-z_]+(?:\.[a-z_]+)?)"', f.read_text()))
    used -= {t.name for t in PACK.terms}
    assert used and used <= ids, sorted(used - ids)


def test_no_module_level_import_of_the_extractors_package():
    for f in PKG.glob("*.py"):
        for line in f.read_text().splitlines():
            assert not re.match(r"(from|import) repo_intel\.extractors", line), (f.name, line)


def test_other_xml_is_left_alone():
    for data in (b'<?xml version="1.0"?><project><modelVersion>4</modelVersion></project>', b"<root>POWERMART</root>", b"not xml"):
        out = export(path="pom.xml", data=data)
        assert not out.parsed and out.observations == [] and out.diagnostics == []


# ---------------------------------------------------------------------- the sales export


def test_workflow_sessions_and_links():
    out = export()
    assert out.parsed
    ents, rels = entities(out), relations(out)
    wf = ents[job()]
    assert wf["platform_kind"] == "infa:Workflow" and wf["schedule"] == "custom" and wf["parameter_file"] == "wf_daily_sales.par" and wf["entry"] == ["s_m_load_sales"]
    for name, kind in (("s_m_load_sales", "session"), ("s_m_summarise", "session"), ("dec_any_rows", "decision"), ("cmd_push_extract", "command"), ("em_failure", "email")):
        assert ents[task(name)]["type"] == kind
        assert (job(), "CONTAINS", task(name)) in rels
    assert ents[task("s_m_load_sales")]["reusable"] is True and "reusable" not in ents[task("s_m_summarise")]
    assert rels[(task("s_m_load_sales"), "RUNS", prog("SALES/m_load_sales"))]
    on = {(s[1], o[1]): q.get("on") for (s, p, o), q in rels.items() if p == "DEPENDS_ON"}
    assert on[(task("em_failure")[1], task("s_m_load_sales")[1])] == "failure"
    assert on[(task("dec_any_rows")[1], task("s_m_load_sales")[1])] == "success"
    assert on[(task("s_m_summarise")[1], task("dec_any_rows")[1])] == "expression"
    assert (task("s_m_load_sales"), "REFERENCES_CONFIG", cfg("connection:ORA_SRC")) in rels
    assert rels[(task("s_m_load_sales"), "REFERENCES_CONFIG", cfg("parameter:$DBConnection_DW"))]["resolved_by"] == "parameter_file"
    assert cfg("connection:ORA_DW") not in ents and cfg("connection:$Target") not in ents
    check_contract(out, admitted(layer_bytes(SALES)))


def test_definitions_and_mapping_lineage():
    out = export()
    ents, rels = entities(out), relations(out)
    m = ents[prog("SALES/m_load_sales")]
    assert m["platform_kind"] == "infa:Mapping" and m["parameters"] == ["$$LOAD_DATE"] and m["variables"] == ["$$ROWS_LOADED"]
    for t in ("src.orders", "src.customers"):
        assert (prog("SALES/m_load_sales"), "READS_FROM", table(t)) in rels
    for t in ("dw_sales", "dw_high_value"):
        assert (prog("SALES/m_load_sales"), "WRITES_TO", table(t)) in rels
    assert table("orders") not in ents
    flat = key("DataSchema", kind="record_layout", name="informatica:SALES/unknown_customers")
    assert ents[flat]["role"] == "target" and ents[flat]["fields"] == 2
    assert (flat, "CONTAINS", key("Field", table="informatica:SALES/unknown_customers", name="CUSTOMER_ID")) in rels


def test_load_mapping_transforms():
    shape = unit_of(export(), "transform", "SALES/m_load_sales")
    assert shape["language"] == "informatica"
    read, source_filter, lookup_read, join, derive, valid, high, rest = shape["transforms"]
    assert read["inputs"] == ["src.orders"] and source_filter["predicate"] == {"sql": "ORDERS.STATUS <> 'CANCELLED'"}
    assert join["inputs"] == [source_filter["id"], lookup_read["id"]] and lookup_read["inputs"] == ["src.customers"]
    assert join["join"] == {"type": "left", "on": [["customer_id", "customer_id"]]} and join["columns"] == {"add": ["region"]}
    assert derive["inputs"] == [join["id"]], "the expression reads the lookup's result, not the source qualifier's"
    assert derive["columns"]["add"] == ["net_amount", "region_code", "load_date"]
    assert valid["op"] == "filter" and "primitive" not in valid
    assert (high["output"], rest["output"]) == ("dw_high_value", "dw_sales")
    assert rest["predicate"] == {"e": "unop", "op": "not", "operand": high["predicate"]} and high["primitive"] == rest["primitive"] == "split_by_condition"


def test_summary_mapping_transforms_bind_mapping_parameters():
    shape = unit_of(export(), "transform", "SALES/m_summarise")
    override = shape["transforms"][0]
    assert override["inputs"] == ["dw_sales"] and ":LOAD_DATE" in override["predicate"]["sql"] and "$$" not in json.dumps(shape)
    (agg,) = steps(shape, "aggregate")
    assert agg["keys"] == ["region_code"] and agg["measures"][0] == {"fn": "sum", "as": "total_amount", "column": "net_amount"}
    (sort,) = steps(shape, "sort")
    assert sort["order"] == [{"column": "total_amount", "desc": True}] and sort["output"] == "sales_summary"


def test_flow_units_validate_and_lower_ports():
    out = export()
    unit = unit_of(out, "behavior", "SALES/m_load_sales")
    assert unit["kind"] == "flow" and unit["language"] == "informatica"
    (loop,) = unit["body"]
    assert loop["op"] == "loop" and loop["kind"] == "implicit"
    assigned = {s["target"]["name"] for s in loop["body"] if s["op"] == "assign"}
    assert {"v_DISCOUNT", "NET_AMOUNT"} <= assigned


def test_unmapped_constructs_are_counted():
    counts = constructs(export())
    for cid in ("infa:task.command", "infa:task.email"):
        assert counts.get((cid, False)), cid
    assert not any(k[0] == "Program" and "cmd_push_extract" in k[1] for k in entities(export()))


def test_names_never_values():
    for out in (export(), returns(), parameters()):
        text = blob(out)
        for value in NEVER:
            assert value not in text, value


# ---------------------------------------------------------------------- the returns export


def test_returns_mapping_transforms():
    out = returns()
    shape = unit_of(out, "transform", "SALES/m_load_returns")
    (join,) = steps(shape, "join")
    detail, master = join["inputs"]
    by_id = {t["id"]: t for t in shape["transforms"]}
    assert by_id[detail]["op"] == "sort" and by_id[by_id[detail]["inputs"][0]]["inputs"] == ["src.orders"]
    assert by_id[master]["op"] == "deduplicate" and by_id[master]["candidates"] == ["dedupe_by_hash"]
    assert join["join"] == {"type": "inner", "on": [["order_id", "order_id"]]}
    (update,) = steps(shape, "platform")
    assert update["candidates"] == ["merge_into"] and update["output"] == "dw_returns" and update["construct"] == "infa:transformation.update_strategy"
    assert "ref.return_reasons" not in json.dumps(shape), "an unconnected lookup has no row stream to join"
    rels = relations(out)
    assert rels[(prog("SALES/m_load_returns"), "READS_FROM", table("ref.return_reasons"))] == {"via": "lookup"}
    assert constructs(out).get(("infa:transformation.lookup_unconnected", True)) == 1
    check_contract(out, admitted(layer_bytes(RETURNS, "commits/01-returns-mapping")))


def test_returns_workflow_and_session_override():
    out = returns()
    ents = entities(out)
    assert "parameter_file" not in ents[job("SALES/wf_returns")], "a parameter file named through variables is not recorded"
    assert ents[task("ew_returns_file", "SALES/wf_returns")]["type"] == "event_wait"
    assert "session s_m_load_returns overrides Sql Query of instance SQ_ORDERS: the mapping's own value is what the transforms show" in messages(out)


# ---------------------------------------------------------------------- parameter files


def test_parameter_file_names_without_values():
    out = parameters()
    assert out.parsed
    ents, rels = entities(out), relations(out)
    assert ents[cfg("parameter:$$LOAD_DATE")]["kind"] == "mapping_parameter"
    assert ents[cfg("parameter:$DBConnection_DW")]["kind"] == "session_parameter"
    assert cfg("parameter:$PMSessionLogCount") not in ents and cfg("parameter:$PMSessionLogFile") not in ents
    assert constructs(out)[("infa:parameter_builtin", False)] == 2
    assert rels[(job(), "REFERENCES_CONFIG", cfg("parameter:$$REGION_FILTER"))] == {"via": "parameter_file"}
    assert (task("s_m_summarise"), "REFERENCES_CONFIG", cfg("parameter:$DBConnection_DW")) in rels
    for o in out.observations:
        if o.kind == "relation":
            assert o.payload["subject"].get("declare") is False
    check_contract(out, admitted(layer_bytes(PARAMS)))


def test_other_par_files_and_odd_sections():
    out = parameters(b"width=10\nheight=20\n", path="conf/size.par")
    assert not out.parsed and out.observations == [] and out.diagnostics == []
    out = parameters(b"[SALES.s_load]\n$$X=1\n[nofolder]\n$$Y=2\n")
    assert out.parsed
    assert any("names a session without its workflow" in m for m in messages(out))
    assert any("names no folder" in m for m in messages(out))
    assert not any(o.kind == "relation" for o in out.observations)


# ---------------------------------------------------------------------- refusals and small cases


def test_internal_dtd_subset_is_refused():
    data = edited(b'<!DOCTYPE POWERMART SYSTEM "powrmart.dtd">', b'<!DOCTYPE POWERMART [<!ENTITY a "aaaa">]>')
    out = export(data=data)
    assert not out.parsed and out.observations == []
    assert len(messages(out)) == 1 and "internal DTD or entity declarations" in messages(out)[0]


def test_malformed_export():
    out = export(data=b'<?xml version="1.0"?><POWERMART><REPOSITORY><FOLDER NAME="X">')
    assert not out.parsed and len(messages(out)) == 1 and messages(out)[0].startswith(f"PowerCenter export {SALES}: ")


def test_lookup_table_from_a_parameter_stays_dynamic():
    out = export(data=edited(b'VALUE="SRC.CUSTOMERS"', b'VALUE="$$CUSTOMER_TABLE"'))
    assert table("src.customers") not in entities(out)
    assert "lookup LKP_CUSTOMER of mapping SALES/m_load_sales reads a table named by $$CUSTOMER_TABLE: not resolved" in messages(out)
    shape = unit_of(out, "transform", "SALES/m_load_sales")
    assert any(t["op"] == "read" and t["inputs"] == ["{customer_table}"] for t in shape["transforms"])
    check_contract(out, admitted(edited(b'VALUE="SRC.CUSTOMERS"', b'VALUE="$$CUSTOMER_TABLE"')))


def test_shortcuts_unresolved_instances_and_mapplets():
    data = edited(b'<SOURCE BUSINESSNAME="" DATABASETYPE="Oracle" DBDNAME="ORA_SRC" DESCRIPTION="" NAME="ORDERS"',
                  b'<SHORTCUT NAME="SC_CALENDAR" FOLDERNAME="SHARED" OBJECTTYPE="SOURCE" REFOBJECTNAME="CALENDAR" REPOSITORYNAME="REP_ETL"/>\n'
                  b'    <SOURCE BUSINESSNAME="" DATABASETYPE="Oracle" DBDNAME="ORA_SRC" DESCRIPTION="" NAME="ORDERS"')
    data = data.replace(b'<INSTANCE DESCRIPTION="" NAME="LKP_CUSTOMER"',
                        b'<INSTANCE NAME="MPLT_CLEAN" TRANSFORMATION_NAME="MPLT_CLEAN" TRANSFORMATION_TYPE="Mapplet" TYPE="MAPPLET"/>\n'
                        b'        <INSTANCE NAME="SC_CALENDAR" TRANSFORMATION_NAME="SC_CALENDAR" TRANSFORMATION_TYPE="Source Definition" TYPE="SOURCE"/>\n'
                        b'        <INSTANCE DESCRIPTION="" NAME="LKP_CUSTOMER"', 1)
    out = export(data=data)
    msgs = messages(out)
    assert "shortcut SC_CALENDAR in folder SALES points to an object of folder SHARED: not in this export" in msgs
    assert "mapping SALES/m_load_sales uses source SC_CALENDAR, whose definition is not in this export: not linked" in msgs
    counts = constructs(out)
    assert counts.get(("infa:mapplet_instance", False)) == 1 and counts.get(("infa:instance_unresolved", False)) == 1
    check_contract(out, admitted(data))


def test_windows_1252_export():
    data = layer_bytes(SALES).replace(b'encoding="UTF-8"', b'encoding="windows-1252"', 1).replace(b'DESCRIPTION="The daily sales load"', b'DESCRIPTION="Caf\xe9 sales load"', 1)
    out = export(data=data)
    assert out.parsed and job() in entities(out)


def test_output_is_deterministic():
    for make in (export, returns, parameters):
        a, b = make(), make()
        assert [json.dumps(o.payload, sort_keys=True, default=str) for o in a.observations] == [json.dumps(o.payload, sort_keys=True, default=str) for o in b.observations]
        assert messages(a) == messages(b)
