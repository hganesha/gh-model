"""Ab Initio (repo_intel/platforms/abinitio): DML record formats read into DataSchemas of
Fields, graphs read from the mp commands of their deployed scripts under
.rkp/inputs/abinitio/, and binary graphs and transforms inventoried as coverage gaps."""

import hashlib
import json
import re
from pathlib import Path

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.compile.transforms import validate_shape
from repo_intel.platforms import pack_extractors, packs, validate_pack
from repo_intel.platforms.abinitio import PACK, dml, graph, source, vocabulary
from repo_intel.platforms.abinitio.source import DmlRecordFormat, GraphScript, Unread

DML_PATH = "sandbox/dml/customers.dml"
GRAPH_PATH = ".rkp/inputs/abinitio/load_customers.ksh"

CUSTOMERS_DML = b'''// customers, as the landing file holds them
include "/~$AI_DML/common_types.dml";
type address_t = record
  string(",") street;
  string(",") city;
end;
record
  decimal(",") cust_id;
  utf8 string(",") cust_name = "not-a-real-default-0000";
  string(2) country_code;
  date("YYYY-MM-DD")(",") signup_date = NULL;
  decimal(10.2) balance;
  unsigned integer(4) visits;
  string(",")[3] phones;
  address_t address;
  record
    string(",") kind;
    decimal("\\n") score;
  end rating;
end;
'''

LOAD_GRAPH = b'''#!/bin/ksh
# deployed script of load_customers.mp
export AI_DB_PASSWORD=not-a-real-secret-0000
mp job load_customers
mp ifile in_customers file:/data/landing/customers.dat -metadata dml/customers.dml
mp reformat clean -transform $AI_XFR/clean.xfr
mp filter-by-expression active -select_expr "status == \\"A\\""
mp sort srt -key "{cust_id; signup_date descending}"
mp dedup-sorted ddp -key "{cust_id}"
mp input-table countries -select "select code, name from dw.country" -dbc_file $AI_DB/etl-host-0000.dbc
mp join jn -key "{country_code}" -join_type inner
mp output-table load -table DW.CUSTOMERS -dbc_file $AI_DB/dw.dbc -password not-a-real-secret-0000
mp ofile rejects file:$AI_SERIAL/rejects.dat
mp straight-flow in_customers.read clean.in
mp straight-flow clean.out active.in
mp straight-flow active.out srt.in
mp straight-flow active.deselect rejects.write
mp straight-flow srt.out ddp.in
mp straight-flow countries.read jn.in1
mp straight-flow ddp.out jn.in0
mp straight-flow jn.out load.write
mp run
'''


def run(data: bytes, path: str):
    for extractor in (DmlRecordFormat(), GraphScript(), Unread()):
        if extractor.matches(path):
            return extractor.extract(path, data, hashlib.sha256(data).hexdigest())
    raise AssertionError(f"no Ab Initio extractor matches {path}")


def ref(x) -> tuple:
    return x if isinstance(x, str) else (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def key(entity_kind: str, **nk) -> tuple:
    return (entity_kind, json.dumps(nk, sort_keys=True))


def task(j: str, name: str) -> tuple:
    return key("Task", framework="abinitio", job=j, name=name)


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


def transforms(out) -> list[dict]:
    found = [o.payload for o in out.observations if o.kind == "transform"]
    assert len(found) == 1
    return found[0]["transforms"]


def check_all_valid(out) -> None:
    assert not [d.message for d in out.diagnostics if d.severity == "warning"]
    for o in out.observations:
        p = o.payload
        if o.kind == "transform":
            assert validate_shape(p) == [], p
            assert p["language"] == "abinitio" and str(o.evidence_class) in ("E1", "EvidenceClass.E1")
        if o.kind == "relation" and isinstance(p["subject"], dict) and isinstance(p["object"], dict):
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p


def fields_of(out, layout: str) -> dict[str, dict]:
    return {json.loads(k[1])["name"]: a for k, a in entities(out).items() if k[0] == "Field" and json.loads(k[1])["table"] == layout}


# ---------------------------------------------------------------------- the pack


def test_registered_as_the_abinitio_pack():
    assert packs()["abinitio"] is PACK
    assert validate_pack(PACK) == []
    assert {e.id for e in PACK.extractors} == {"abinitio.dml", "abinitio.graph", "abinitio.binary"}
    assert PACK.inputs == (".rkp/inputs/abinitio/**",)
    assert {"abi:Graph", "abi:Component", "abi:RecordFormat", "abi:DmlField"} == {t.name for t in PACK.terms}
    assert {e.id for e in pack_extractors()} >= {"abinitio.dml", "abinitio.graph", "abinitio.binary"}


def test_every_construct_and_term_the_readers_name_is_listed():
    ids = {c.id for c in PACK.constructs}
    terms = {t.name for t in PACK.terms}
    pkg = Path(source.__file__).parent
    used = set()
    for name in ("dml.py", "graph.py", "source.py"):
        used |= set(re.findall(r'"(abi:[A-Za-z_][A-Za-z0-9_.-]*)"', (pkg / name).read_text()))
    assert used
    assert not {u for u in used if u not in ids and u not in terms}
    for name in graph.COMPONENTS:
        assert f"abi:component.{name}" in ids
    assert [c.id for c in vocabulary.CONSTRUCTS] == sorted(c.id for c in vocabulary.CONSTRUCTS)


def test_what_each_extractor_matches():
    assert DmlRecordFormat().matches("x/dml/a.dml") and DmlRecordFormat().matches("A.DML")
    assert GraphScript().matches(".rkp/inputs/abinitio/run/load.ksh")
    assert GraphScript().matches("repo/.rkp/inputs/abinitio/load.txt")
    assert not GraphScript().matches("run/load.ksh")
    assert not GraphScript().matches(".rkp/inputs/abinitio/x.dml")
    assert Unread().matches("sandbox/mp/load.mp") and Unread().matches("sandbox/xfr/clean.xfr")
    assert not Unread().matches("music/song.mp") and not Unread().matches("load.mp")


# ---------------------------------------------------------------------- record formats


def test_record_format_fields_and_types():
    out = run(CUSTOMERS_DML, DML_PATH)
    assert out.parsed
    check_all_valid(out)
    layout = f"abinitio:{DML_PATH}"
    ents = entities(out)
    schema = key("DataSchema", kind="record_format", name=layout)
    assert ents[schema]["platform_kind"] == "abi:RecordFormat" and ents[schema]["fields"] == 11
    f = fields_of(out, layout)
    assert list(f) == ["cust_id", "cust_name", "country_code", "signup_date", "balance", "visits", "phones", "address",
                       "rating", "rating.kind", "rating.score"]
    assert f["cust_id"] == {**f["cust_id"], "type": "decimal", "delimited": True, "position": 1}
    assert f["cust_name"]["charset"] == "utf8" and "nullable" not in f["cust_name"]
    assert f["country_code"]["length"] == 2 and "delimited" not in f["country_code"]
    assert f["signup_date"]["type"] == "date" and f["signup_date"]["format"] == "YYYY-MM-DD" and f["signup_date"]["nullable"] is True
    assert (f["balance"]["length"], f["balance"]["scale"]) == (10, 2)
    assert f["visits"]["type"] == "integer" and f["visits"]["unsigned"] is True and f["visits"]["length"] == 4
    assert f["phones"]["vector"] == 3
    assert f["address"]["type"] == "named:address_t"
    assert f["rating"]["type"] == "record" and f["rating.score"]["position"] == 11
    for name in f:
        assert (schema, "CONTAINS", key("Field", table=layout, name=name)) in relations(out)


def test_named_types_are_their_own_record_formats():
    out = run(CUSTOMERS_DML, DML_PATH)
    layout = f"abinitio:{DML_PATH}#address_t"
    assert entities(out)[key("DataSchema", kind="record_format", name=layout)]["type_name"] == "address_t"
    assert set(fields_of(out, layout)) == {"street", "city"}
    c = constructs(out)
    assert c[("abi:record_format", True)] == 1 and c[("abi:type_definition", True)] == 1


def test_includes_are_counted_not_followed():
    out = run(CUSTOMERS_DML, DML_PATH)
    assert constructs(out)[("abi:dml_include", False)] == 1
    assert any("include" in d.message and "not followed" in d.message for d in out.diagnostics)
    assert not any("common_types" in json.dumps(o.payload) for o in out.observations)


def test_default_values_are_never_copied():
    out = run(CUSTOMERS_DML, DML_PATH)
    assert "not-a-real-default-0000" not in json.dumps([o.payload for o in out.observations])


def test_metadata_type_and_decimal_with_comma_scale():
    text = b'metadata type =\nrecord\n  decimal(12, 3) amount;\n  datetime("YYYY-MM-DD HH24:MI:SS") at;\n  string("\\x01", maximum_length=20) note;\nend;\n'
    out = run(text, "a/orders.dml")
    f = fields_of(out, "abinitio:a/orders.dml")
    assert (f["amount"]["length"], f["amount"]["scale"]) == (12, 3)
    assert f["at"]["type"] == "datetime" and f["at"]["format"] == "YYYY-MM-DD HH24:MI:SS"
    assert f["note"]["type"] == "string" and f["note"]["delimited"] is True


def test_conditional_fields_and_unknown_statements_are_counted():
    text = b'let integer(4) k = 3;\nrecord\n  string(",") a;\n  if (a == "x") string(",") b;\n  ;\nend;\n'
    out = run(text, "a/c.dml")
    assert set(fields_of(out, "abinitio:a/c.dml")) == {"a"}
    assert constructs(out)[("abi:dml_unparsed", False)] == 2


def test_type_alias_is_counted():
    out = run(b"include \"common.dml\";\nmetadata type = customer_t;\n", "a/alias.dml")
    c = constructs(out)
    assert c[("abi:type_alias", False)] == 1 and c[("abi:dml_include", False)] == 1
    assert not [o for o in out.observations if o.kind == "entity"]


def test_a_record_without_end_is_not_read():
    out = run(b"record\n  string(\",\") a;\n", "a/bad.dml")
    assert not out.parsed and "no end" in out.diagnostics[0].message
    assert not out.observations


def test_record_format_spans_are_byte_spans():
    text = "// café\nrecord\n  string(\",\") name;\nend;\n".encode()
    out = run(text, "a/u.dml")
    field = next(o for o in out.observations if o.kind == "entity" and o.payload["kind"] == "Field")
    sp = field.evidence.span
    assert text[sp.start_byte:sp.end_byte] == b'string(",") name;'


def test_parser_tokens_skip_comments():
    toks = dml.tokens('/* a\nb */ record // x\n end;')
    assert [t.text for t in toks] == ["record", "end", ";"]


# ---------------------------------------------------------------------- deployed graphs


def test_graph_components_and_flows():
    out = run(LOAD_GRAPH, GRAPH_PATH)
    assert out.parsed
    check_all_valid(out)
    ents = entities(out)
    job = key("Job", framework="abinitio", name="load_customers")
    assert ents[job]["platform_kind"] == "abi:Graph"
    labels = ("in_customers", "clean", "active", "srt", "ddp", "countries", "jn", "load", "rejects")
    for label in labels:
        assert (job, "CONTAINS", task("load_customers", label)) in relations(out)
    assert ents[task("load_customers", "clean")]["family"] == "xfr"
    assert ents[task("load_customers", "in_customers")]["record_format"] == "dml/customers.dml"
    rels = relations(out)
    assert (task("load_customers", "jn"), "DEPENDS_ON", task("load_customers", "ddp")) in rels
    assert (task("load_customers", "rejects"), "DEPENDS_ON", task("load_customers", "active")) in rels
    assert (task("load_customers", "in_customers"), "READS_FROM", key("Dataset", name="/data/landing/customers.dat")) in rels
    assert (task("load_customers", "countries"), "READS_FROM", key("Table", datastore="default", name="dw.country")) in rels
    assert (task("load_customers", "load"), "WRITES_TO", key("Table", datastore="default", name="dw.customers")) in rels


def test_graph_transform_shapes():
    out = run(LOAD_GRAPH, GRAPH_PATH)
    t = transforms(out)
    ops = [x["op"] for x in t]
    assert ops[0] == "read" and t[0]["inputs"] == ["/data/landing/customers.dat"]
    reformat = next(x for x in t if x["op"] == "platform")
    assert reformat["construct"] == "abi:component.reformat"
    flt = next(x for x in t if x["op"] == "filter")
    assert flt["primitive"] == "split_by_condition"
    srt = next(x for x in t if x["op"] == "sort")
    assert srt["order"] == [{"column": "cust_id", "desc": False}, {"column": "signup_date", "desc": True}]
    ddp = next(x for x in t if x["op"] == "deduplicate")
    assert ddp["keys"] == ["cust_id"] and ddp["primitive"] == "dedupe_by_key"
    jn = next(x for x in t if x["op"] == "join")
    assert jn["join"] == {"type": "inner", "on": [["country_code", "country_code"]]}
    assert jn["inputs"][0] == ddp["id"]  # in0 before in1, whatever the flow order
    pubs = {x["output"]: x for x in t if x["op"] == "publish"}
    assert pubs["dw.customers"]["inputs"] == [jn["id"]]
    assert pubs["{AI_SERIAL}/rejects.dat"]["dynamic"] is True


def test_parameter_paths_stay_dynamic():
    out = run(LOAD_GRAPH, GRAPH_PATH)
    assert not [k for k in entities(out) if "AI_SERIAL" in k[1]]
    assert constructs(out)[("abi:dynamic_name", False)] == 1
    assert any("rejects" in d.message and "parameters" in d.message for d in out.diagnostics)
    summary = next(o.payload for o in out.observations if o.kind == "lineage_summary")
    assert summary == {"resolved": 3, "unresolved": 1}


def test_values_are_never_copied():
    blob = json.dumps([o.payload for o in run(LOAD_GRAPH, GRAPH_PATH).observations])
    for probe in ("not-a-real-secret-0000", "etl-host-0000", "AI_DB_PASSWORD", "dw.dbc", "status =="):
        assert probe not in blob


def test_construct_counts():
    c = constructs(run(LOAD_GRAPH, GRAPH_PATH))
    assert c[("abi:graph", True)] == 1 and c[("abi:flow", True)] == 8
    assert c[("abi:component.reformat", False)] == 1 and c[("abi:transform_ref", False)] == 1
    for name in ("ifile", "ofile", "sort", "dedup-sorted", "join", "input-table", "output-table", "filter-by-expression"):
        assert c[(f"abi:component.{name}", True)] == 1


def test_unknown_commands_bad_flows_and_duplicate_labels():
    text = b"mp job g\nmp ifile a file:/d/a.dat\nmp frobnicate x\nmp ifile a file:/d/b.dat\nmp straight-flow a.read nowhere.in\nmp straight-flow a.read\n"
    out = run(text, ".rkp/inputs/abinitio/g.ksh")
    c = constructs(out)
    assert c[("abi:command_unknown", False)] == 1
    assert c[("abi:component.ifile", False)] == 1  # the second `a`
    assert c[("abi:flow", False)] == 2
    assert key("Dataset", name="/d/a.dat") in entities(out) and key("Dataset", name="/d/b.dat") not in entities(out)


def test_graph_name_from_a_parameter_is_not_read():
    out = run(b"mp job $GRAPH\nmp ifile a file:/d/a.dat\n", ".rkp/inputs/abinitio/g.ksh")
    assert constructs(out) == {("abi:graph", False): 1}
    assert not [o for o in out.observations if o.kind == "entity"]


def test_two_graphs_in_one_script_and_continuation_lines():
    text = b"mp job one\nmp ifile a \\\n  file:/d/a.dat\nmp ofile b file:/d/b.dat\nmp straight-flow a.read b.write\nmp job two\nmp ifile c file:/d/c.dat\n"
    out = run(text, ".rkp/inputs/abinitio/two.ksh")
    rels = relations(out)
    assert (task("one", "a"), "READS_FROM", key("Dataset", name="/d/a.dat")) in rels
    assert (task("one", "b"), "WRITES_TO", key("Dataset", name="/d/b.dat")) in rels
    assert (key("Job", framework="abinitio", name="two"), "CONTAINS", task("two", "c")) in rels
    assert len([o for o in out.observations if o.kind == "transform"]) == 2


def test_sql_components_and_parameters_in_sql():
    text = (b'mp job s\n'
            b'mp input-table src -select "select id from $AI_SCHEMA.orders where d = \'${RUN_DATE}\'"\n'
            b'mp run-sql purge -sql "delete from stage.orders_tmp"\n'
            b'mp update-table upd -table sales.orders\n'
            b'mp straight-flow src.read upd.in\n')
    out = run(text, ".rkp/inputs/abinitio/s.ksh")
    check_all_valid(out)
    rels = relations(out)
    assert (task("s", "purge"), "WRITES_TO", key("Table", datastore="default", name="stage.orders_tmp")) in rels
    assert (task("s", "upd"), "WRITES_TO", key("Table", datastore="default", name="sales.orders")) in rels
    assert not [k for k in entities(out) if k[0] == "Table" and "orders" in k[1] and "schema" in k[1].lower()]
    pub = next(x for x in transforms(out) if x["op"] == "publish")
    assert pub["candidates"] == ["merge_into"]


def test_aggregate_union_and_partition_components():
    text = (b"mp job u\nmp ifile a file:/d/a.dat\nmp ifile b file:/d/b.dat\nmp partition-by-key p -key \"{k}\"\n"
            b"mp concatenate cat\nmp rollup r -key \"{k}\" -transform r.xfr\nmp ofile o file:/d/o.dat\n"
            b"mp straight-flow a.read p.in\nmp straight-flow p.out cat.in0\nmp straight-flow b.read cat.in1\n"
            b"mp straight-flow cat.out r.in\nmp straight-flow r.out o.write\n")
    out = run(text, ".rkp/inputs/abinitio/u.ksh")
    check_all_valid(out)
    t = transforms(out)
    union = next(x for x in t if x["op"] == "union")
    assert len(union["inputs"]) == 2
    agg = next(x for x in t if x["op"] == "aggregate")
    assert agg["keys"] == ["k"] and agg["inputs"] == [union["id"]]
    assert next(x for x in t if x["op"] == "publish")["inputs"] == [agg["id"]]


def test_a_script_without_a_graph_is_not_read():
    out = run(b"#!/bin/ksh\necho hello\n", ".rkp/inputs/abinitio/notes.txt")
    assert not out.parsed and not out.observations


def test_graph_spans_are_byte_spans():
    text = "# café\nmp job g\nmp ifile a file:/d/a.dat\n".encode()
    out = run(text, ".rkp/inputs/abinitio/g.ksh")
    task_obs = next(o for o in out.observations if o.kind == "entity" and o.payload["kind"] == "Task")
    sp = task_obs.evidence.span
    assert text[sp.start_byte:sp.end_byte] == b"mp ifile a file:/d/a.dat"


def test_deterministic():
    a = run(LOAD_GRAPH, GRAPH_PATH)
    b = run(LOAD_GRAPH, GRAPH_PATH)
    assert [o.payload for o in a.observations] == [o.payload for o in b.observations]
    c = run(CUSTOMERS_DML, DML_PATH)
    d = run(CUSTOMERS_DML, DML_PATH)
    assert [o.payload for o in c.observations] == [o.payload for o in d.observations]


def test_key_and_options_helpers():
    assert graph.key_columns("{a; b descending, c}") == ["a", "b", "c"]
    assert graph.descending("{a; b descending}") == [False, True]
    assert graph.options(["-key", "{a}", "-flag", "-table", "t"]) == {"key": "{a}", "flag": "", "table": "t"}
    assert graph.path_name("file:${AI_SERIAL}/x.dat") == ("{AI_SERIAL}/x.dat", True)
    assert graph.path_name("mfile://etl-host-0000/data/x.dat") == ("/data/x.dat", False)
    assert graph.path_name("file:/data/x.dat") == ("/data/x.dat", False)


# ---------------------------------------------------------------------- gaps


def test_binary_graphs_and_transforms_are_gaps():
    mp = run(b"\x00\x01binary", "sandbox/mp/load_customers.mp")
    assert constructs(mp) == {("abi:graph_binary", False): 1}
    assert ".rkp/inputs/abinitio/" in mp.diagnostics[0].message
    xfr = run(b"out :: reformat(in) = begin out.* :: in.*; end;", "sandbox/xfr/clean.xfr")
    assert constructs(xfr) == {("abi:transform_file", False): 1}
    assert "not lowered" in xfr.diagnostics[0].message
