"""The Hadoop pack (repo_intel/platforms/hadoop/): Hive and Impala scripts, Pig Latin, Oozie
workflows, coordinators and bundles, and Sqoop commands, each read from one file's bytes."""

import ast
import hashlib
import json
import re
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import validate_unit
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.compile.transforms import validate_shape
from repo_intel.ingest import secrets
from repo_intel.platforms import packs, validate_pack
from repo_intel.platforms.hadoop import PACK
from repo_intel.platforms.hadoop.common import Substituted, dataset_path, resolve_relative, table_name
from repo_intel.platforms.hadoop.hive import HiveScripts
from repo_intel.platforms.hadoop.oozie import OozieDefinitions, frequency
from repo_intel.platforms.hadoop.pig import PigScripts, parse_expr
from repo_intel.platforms.hadoop.sqoop import SqoopOptionsFiles

SOURCES = sorted((Path(__file__).resolve().parents[1] / "repo_intel" / "platforms" / "hadoop").glob("*.py"))
FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "F34-hadoop-oozie"
WF = "apps/daily_orders/workflow.xml"
COORD = "apps/daily_orders/coordinator.xml"
LOAD = "apps/daily_orders/hive/load_orders.hql"
SUMMARY = "apps/daily_orders/hive/daily_summary.q"
PIG = "apps/daily_orders/pig/clean_customers.pig"
OPTS = "sqoop/import_products.opts"
EXTRACTORS = {e.id: e for e in PACK.extractors}


def run(path: str, data: bytes | str, extractor=None):
    raw = data.encode() if isinstance(data, str) else data
    x = extractor or next(e for e in PACK.extractors if e.matches(path))
    return x.extract(path, raw, hashlib.sha256(raw).hexdigest())


def fixture(path: str, layer: str = "base") -> bytes:
    return (FIXTURE / layer / path).read_bytes()


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


def relations(out) -> dict[tuple, dict]:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])): o.payload["qualifiers"] for o in out.observations if o.kind == "relation"}


def relation_obs(out, predicate: str) -> list:
    return [o for o in out.observations if o.kind == "relation" and o.payload["predicate"] == predicate]


def constructs(out) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = {}
    for o in out.observations:
        if o.kind == "construct":
            k = (o.payload["construct"], o.payload["modelled"])
            counts[k] = counts.get(k, 0) + 1
    return counts


def transforms(out) -> list[dict]:
    return [o.payload for o in out.observations if o.kind == "transform"]


def behaviors(out) -> list[dict]:
    return [o.payload for o in out.observations if o.kind == "behavior"]


def messages(out) -> list[str]:
    return [d.message for d in out.diagnostics]


def text_of(out) -> str:
    return json.dumps([o.payload for o in out.observations], default=str) + json.dumps(messages(out))


def task(name: str, job: str = WF) -> tuple:
    return key("Task", framework="oozie", job=job, name=name)


def job(path: str) -> tuple:
    return key("Job", framework="oozie", name=path)


def table(name: str) -> tuple:
    return key("Table", datastore="default", name=name)


def ds(name: str) -> tuple:
    return key("Dataset", name=name)


def prog(platform: str, name: str) -> tuple:
    return key("Program", platform=platform, name=name)


def assert_valid(out) -> None:
    """Every relation is allowed by the ontology; every transform and behaviour unit validates."""
    for o in out.observations:
        if o.kind == "relation":
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p
        if o.kind == "transform":
            assert validate_shape(o.payload) == [], o.payload
        if o.kind == "behavior":
            assert validate_unit(o.payload) == [], o.payload
        s = o.evidence.span
        assert 0 <= s.start_byte <= s.end_byte


ALL_BASE = [WF, COORD, LOAD, SUMMARY, PIG, OPTS, "ddl/create_tables.hql"]


# ---------------------------------------------------------------------- registration


def test_registered_and_valid():
    assert packs()["hadoop"] is PACK
    assert (PACK.id, PACK.prefix, PACK.version) == ("hadoop", "hdp", "1.0.0")
    assert validate_pack(PACK) == []
    assert set(EXTRACTORS) == {"hadoop.hive", "hadoop.pig", "hadoop.oozie", "hadoop.sqoop"}
    terms = {t.name for t in PACK.terms}
    assert {"hdp:HiveScript", "hdp:PigScript", "hdp:SqoopJob", "hdp:OozieWorkflow", "hdp:OozieAction", "hdp:OozieCoordinator", "hdp:OozieBundle"} <= terms


def test_every_construct_the_readers_name_is_listed():
    listed = {c.id for c in PACK.constructs}
    used: set[str] = set()
    for source in SOURCES:
        used |= set(re.findall(r'"(hdp:[a-z][a-z0-9_]*)"', source.read_text()))
    assert used and used <= listed, sorted(used - listed)


def test_extractor_modules_are_imported_only_inside_functions():
    for source in SOURCES:
        tree = ast.parse(source.read_text())
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("repo_intel.extractors"), (source.name, node.module)
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("repo_intel.extractors") for a in node.names), source.name


def test_which_files_each_extractor_claims():
    hive, pig, oozie, sqoop = HiveScripts(), PigScripts(), OozieDefinitions(), SqoopOptionsFiles()
    assert hive.matches("etl/a.hql") and hive.matches("etl/A.HQL")
    assert hive.matches("etl/a.q") and hive.matches("etl/a.sql")  # read only when the text is HiveQL
    assert not hive.matches("etl/a.py")
    assert pig.matches("x/clean.pig") and not pig.matches("x/clean.pl")
    assert oozie.matches("apps/wf/workflow.xml") and oozie.matches("apps/x/any-name.xml")
    assert sqoop.matches("sqoop/import.opts") and sqoop.matches("jobs/sqoop_import.txt") and not sqoop.matches("conf/import.opts")


def test_sql_and_q_files_are_read_only_when_they_are_hiveql():
    plain = "SELECT id FROM customers;\n"
    assert run("db/report.sql", plain, HiveScripts()).observations == []
    assert run("db/report.q", plain, HiveScripts()).observations == []
    assert prog("hive", "hive/report.q") in entities(run("hive/report.q", plain, HiveScripts()))
    hiveql = "INSERT OVERWRITE TABLE mart.t PARTITION (dt='x') SELECT id FROM raw.t;\n"
    assert prog("hive", "db/load.sql") in entities(run("db/load.sql", hiveql, HiveScripts()))
    assert run("db/migrations/V1__init.sql", hiveql, HiveScripts()).observations == []
    assert run("models/m.sql", "INSERT OVERWRITE TABLE t SELECT * FROM {{ ref('x') }}", HiveScripts()).observations == []


def test_xml_that_is_not_oozie_is_left_alone():
    out = run("pom.xml", "<project><artifactId>app</artifactId></project>", OozieDefinitions())
    assert out.observations == [] and not out.parsed


# ---------------------------------------------------------------------- the fixture, file by file


@pytest.mark.parametrize("path", ALL_BASE)
def test_fixture_files_parse_and_validate(path):
    out = run(path, fixture(path))
    assert out.parsed and out.observations
    assert_valid(out)


def test_workflow_jobs_tasks_and_order():
    out = run(WF, fixture(WF))
    ents, rels = entities(out), relations(out)
    wf = ents[job(WF)]
    assert wf["platform_kind"] == "hdp:OozieWorkflow" and wf["app_name"] == "daily-orders-{env}"
    assert wf["parameters"] == ["env", "runDate"]
    names = ["import-orders", "prepare-orders", "clean-customers", "daily-summary", "export-summary", "publish-marker", "notify-failure"]
    for n in names:
        assert (job(WF), "CONTAINS", task(n)) in rels
    assert ents[task("daily-summary")]["type"] == "hive2"
    assert rels[(task("prepare-orders"), "DEPENDS_ON", task("import-orders"))] == {"on": "ok"}
    assert rels[(task("clean-customers"), "DEPENDS_ON", task("prepare-orders"))] == {"on": "ok", "via": "fork"}
    assert rels[(task("export-summary"), "DEPENDS_ON", task("daily-summary"))] == {"on": "ok", "via": "join+decision"}
    assert rels[(task("export-summary"), "DEPENDS_ON", task("clean-customers"))] == {"on": "ok", "via": "join+decision"}
    assert rels[(task("notify-failure"), "DEPENDS_ON", task("import-orders"))] == {"on": "error"}
    # Nothing runs before the first action, and the kill and end nodes are not tasks.
    assert not any(s == task("import-orders") and p == "DEPENDS_ON" for s, p, _ in rels)
    assert not any(k[0] == "Task" and ('"fail"' in k[1] or '"end"' in k[1]) for k in ents)


def test_workflow_actions_run_their_scripts():
    rels = relations(run(WF, fixture(WF)))
    assert (task("prepare-orders"), "RUNS", prog("hive", LOAD)) in rels
    assert (task("daily-summary"), "RUNS", prog("hive", SUMMARY)) in rels
    assert (task("clean-customers"), "RUNS", prog("pig", PIG)) in rels
    assert (task("publish-marker"), "RUNS", key("Artifact", path="apps/daily_orders/scripts/publish_marker.sh")) in rels


def test_workflow_references_to_scripts_do_not_declare_them():
    out = run(WF, fixture(WF))
    for o in relation_obs(out, "RUNS"):
        assert o.payload["object"].get("declare") is False


def test_workflow_flow_unit():
    out = run(WF, fixture(WF))
    units = behaviors(out)
    assert len(units) == 1
    unit = units[0]
    assert unit["kind"] == "flow" and ref(unit["owner"]) == job(WF) and validate_unit(unit) == []
    ops = [s["op"] for s in unit["body"]]
    assert ops == ["platform", "try", "try", "platform", "try", "try", "platform", "if", "try", "try", "try", "throw", "exit"]
    first = unit["body"][1]
    assert first["body"][0]["call"]["callee"]["name"] == "import-orders"
    assert first["catches"][0]["body"][0]["to"] == "notify-failure"
    decision = unit["body"][7]
    assert decision["cond"]["construct"] == "hdp:oozie_decision" and "fs:fileSize" in decision["cond"]["text"]
    assert decision["else"][0]["to"] == "end"


def test_workflow_sqoop_actions_are_dataset_moves():
    out = run(WF, fixture(WF))
    rels = relations(out)
    assert rels[(task("import-orders"), "READS_FROM", table("orders"))] == {"via": "sqoop", "engine": "mysql"}
    assert rels[(task("import-orders"), "WRITES_TO", ds("/data/raw/orders/{runDate}"))] == {"via": "sqoop", "engine": "mysql", "parameterized": True}
    assert (task("export-summary"), "READS_FROM", table("mart.daily_summary")) in rels
    assert (task("export-summary"), "WRITES_TO", table("daily_summary")) in rels
    shapes = {ref(t["owner"]): t["transforms"] for t in transforms(out)}
    imp = shapes[task("import-orders")]
    assert [t["op"] for t in imp] == ["project", "filter"]
    assert imp[1]["output"] == "/data/raw/orders/{runDate}" and imp[1]["dynamic"] is True
    exp = shapes[task("export-summary")]
    assert exp == [{"id": "t1", "op": "publish", "at": exp[0]["at"], "inputs": ["mart.daily_summary"], "output": "daily_summary", "keys": ["order_date", "region"], "primitive": "merge_into"}]


def test_no_connection_string_host_password_recipient_or_value_is_kept():
    for path in ALL_BASE:
        blob = text_of(run(path, fixture(path)))
        for secret in ("fixture-not-a-real-password", "db.example.internal", "hs2.example.internal", "metastore.example.internal",
                       "catalog.example.internal", "jdbc:", "thrift:", "data-oncall", "etl_reader", "/user/etl/", '"prod"', "/data/raw/customers"):
            assert secret not in blob, (path, secret)


def test_reads_a_workflow_masked_at_admission():
    data = fixture(WF)
    findings = secrets.scan(data)
    assert findings, "the planted password must be detected at admission"
    masked = secrets.mask_for_extraction(data, findings)
    out = run(WF, masked)
    assert out.parsed and task("import-orders") in entities(out)
    assert (task("import-orders"), "READS_FROM", table("orders")) in relations(out)


def test_coordinator_schedule_datasets_and_workflow():
    out = run(COORD, fixture(COORD))
    ents, rels = entities(out), relations(out)
    c = ents[job(COORD)]
    assert c["platform_kind"] == "hdp:OozieCoordinator" and c["kind"] == "coordinator"
    assert c["schedule"] == "coord:days(1)" and c["frequency"] == {"every": 1, "unit": "day"} and c["timezone"] == "UTC"
    assert (job(COORD), "READS_FROM", ds("/data/landing/orders/{YEAR}{MONTH}{DAY}")) in rels
    assert (job(COORD), "WRITES_TO", ds("/data/mart/daily_summary/dt={YEAR}-{MONTH}-{DAY}")) in rels
    runs = relation_obs(out, "RUNS")
    assert len(runs) == 1 and ref(runs[0].payload["object"]) == job(WF)
    assert runs[0].evidence_class == "E4" and runs[0].payload["qualifiers"] == {"resolved": "same_directory"}


def test_coordinator_frequencies():
    assert frequency("${coord:hours(6)}") == {"schedule": "coord:hours(6)", "frequency": {"every": 6, "unit": "hour"}}
    assert frequency("60") == {"schedule": "60", "frequency": {"every": 60, "unit": "minute"}}
    assert frequency("0 2 * * *") == {"schedule": "0 2 * * *"}
    assert frequency("${freq}") == {}


def test_coordinator_with_an_unstated_workflow_says_so():
    xml = ('<coordinator-app name="c" frequency="${freq}" start="${s}" end="${e}" timezone="${tz}" xmlns="uri:oozie:coordinator:0.4">'
           '<action><workflow><app-path>${wfPath}</app-path></workflow></action></coordinator-app>')
    out = run("apps/x/coordinator.xml", xml)
    attrs = entities(out)[job("apps/x/coordinator.xml")]
    assert "schedule" not in attrs and "timezone" not in attrs
    assert not relation_obs(out, "RUNS")
    assert "the coordinator's frequency is set at run time" in messages(out)
    assert any("the workflow" in m for m in messages(out))
    assert constructs(out)[("hdp:oozie_coord_action", False)] == 1


def test_bundle_runs_its_coordinators():
    path = "apps/bundle.xml"
    out = run(path, fixture(path, "commits/02-weekly-coordinator"))
    rels = relations(out)
    assert entities(out)[job(path)]["platform_kind"] == "hdp:OozieBundle"
    assert (job(path), "RUNS", job(COORD)) in rels
    assert not any(o == job("apps/daily_orders/weekly_coordinator.xml") for _, _, o in rels)
    assert constructs(out)[("hdp:oozie_bundle_coordinator", False)] == 1


# ---------------------------------------------------------------------- Hive


def test_hive_script_tables_lineage_and_shapes():
    out = run(LOAD, fixture(LOAD))
    ents, rels = entities(out), relations(out)
    p = ents[prog("hive", LOAD)]
    assert p["platform_kind"] == "hdp:HiveScript" and p["databases"] == ["staging"] and p["settings"] == ["hive.exec.dynamic.partition.mode"]
    raw = ents[table("staging.raw_orders")]
    assert raw == {"external": True, "partitioned_by": ["dt"], "stored_as": "textfile"}
    assert ents[key("Field", table="staging.raw_orders", name="dt")] == {"type": "string", "partition": True}
    assert (table("staging.raw_orders"), "DERIVED_FROM", ds("/data/raw/orders")) in rels
    for t in ("mart.open_orders", "mart.closed_orders"):
        assert (prog("hive", LOAD), "WRITES_TO", table(t)) in rels
    assert rels[(prog("hive", LOAD), "WRITES_TO", table("{mart_db}.latest_orders"))] == {"parameterized": True}
    assert ents[table("{mart_db}.latest_orders")] == {"parameterized": True}
    assert "table name made at run time: {mart_db}.latest_orders" in messages(out)
    (unit,) = transforms(out)
    assert ref(unit["owner"]) == prog("hive", LOAD) and unit["language"] == "hadoop"
    last = unit["transforms"][-1]
    assert last["primitive"] == "dedupe_by_key" and last["keys"] == ["order_id"] and last["dynamic"] is True
    assert constructs(out)[("hdp:hive_multi_insert", True)] == 1
    assert constructs(out)[("hdp:hive_msck_repair", False)] == 1


def test_hive_q_file_and_join_aggregate():
    out = run(SUMMARY, fixture(SUMMARY))
    rels = relations(out)
    assert (prog("hive", SUMMARY), "READS_FROM", table("dim.customers")) in rels
    assert (prog("hive", SUMMARY), "WRITES_TO", table("mart.daily_summary")) in rels
    (unit,) = transforms(out)
    assert [t["op"] for t in unit["transforms"]] == ["join", "filter", "aggregate"]
    assert unit["transforms"][2]["measures"] == [{"fn": "count", "column": "*", "as": "orders"}, {"fn": "sum", "column": "amount", "as": "total"}]


def test_hive_load_data_directory_view_and_impala():
    text = ("USE mart;\n"
            "LOAD DATA INPATH '/landing/c/${hivevar:d}' OVERWRITE INTO TABLE stage_c;\n"
            "INSERT OVERWRITE DIRECTORY '/export/c' SELECT id FROM stage_c;\n"
            "CREATE VIEW v_c AS SELECT id FROM stage_c;\n"
            "INVALIDATE METADATA mart.stage_c;\n"
            "COMPUTE STATS mart.stage_c;\n")
    out = run("etl/c.hql", text)
    ents, rels = entities(out), relations(out)
    me = prog("hive", "etl/c.hql")
    assert ents[me]["engine"] == "impala"
    assert rels[(me, "READS_FROM", ds("/landing/c/{d}"))] == {"parameterized": True}
    assert (me, "WRITES_TO", table("mart.stage_c")) in rels
    assert (me, "WRITES_TO", ds("/export/c")) in rels
    assert ents[("DataSchema", json.dumps({"kind": "view", "name": "mart.v_c"}, sort_keys=True))]["reads"] == ["mart.stage_c"]
    counts = constructs(out)
    assert counts[("hdp:hive_load_data", True)] == 1 and counts[("hdp:hive_insert_directory", True)] == 1
    assert counts[("hdp:impala_invalidate_metadata", False)] == 1 and counts[("hdp:impala_compute_stats", False)] == 1
    assert_valid(out)


def test_hive_ctas_and_statement_kinds_it_does_not_model():
    text = ("CREATE TABLE mart.top AS SELECT id, amount FROM mart.orders WHERE amount > 10;\n"
            "ADD JAR /opt/udf.jar;\n"
            "CREATE TEMPORARY FUNCTION f AS 'com.example.F';\n"
            "DROP TABLE IF EXISTS mart.old;\n"
            "ALTER TABLE mart.top ADD COLUMNS (x INT);\n"
            "SELECT TRANSFORM (id) USING 'python s.py' AS (id) FROM mart.orders;\n")
    out = run("etl/d.hql", text)
    rels = relations(out)
    me = prog("hive", "etl/d.hql")
    assert (me, "READS_FROM", table("mart.orders")) in rels and (me, "WRITES_TO", table("mart.top")) in rels
    counts = constructs(out)
    assert counts[("hdp:hive_ctas", True)] == 1
    for cid in ("hdp:hive_add_resource", "hdp:hive_temporary_function", "hdp:hive_drop", "hdp:hive_alter_table", "hdp:hive_transform_script"):
        assert (cid, False) in counts, cid
    assert_valid(out)


def test_hive_substitutions():
    s = Substituted("SELECT * FROM ${hivevar:db}.t WHERE d = ${hivevar:d} AND x = '${y}'")
    assert ":d" in s.text and "'{y}'" in s.text and "${" not in s.text
    token = next(iter(s.tokens))
    assert s.restore(f"{token}.t") == "{db}.t"
    assert s.to_original(s.text.index(":d")) == s.original.index("${hivevar:d}")


# ---------------------------------------------------------------------- Pig


def test_pig_script_shapes():
    out = run(PIG, fixture(PIG))
    ents, rels = entities(out), relations(out)
    p = ents[prog("pig", PIG)]
    assert p["platform_kind"] == "hdp:PigScript" and p["parameters"] == ["INPUT"]
    assert (prog("pig", PIG), "WRITES_TO", table("dim.customers")) in rels
    assert (prog("pig", PIG), "WRITES_TO", ds("/data/mart/top_regions")) in rels
    assert not any(o[0] == "Dataset" and "INPUT" in o[1] for _, _, o in rels)
    assert "relation raw loads a path given at run time ($INPUT)" in messages(out)
    (unit,) = transforms(out)
    ts = unit["transforms"]
    assert ts[0] == {"id": "t1", "op": "read", "at": ts[0]["at"], "inputs": ["{INPUT}"], "dynamic": True}
    assert [t.get("primitive") for t in ts[2:7]] == ["parse_timestamp", "trim_strings", "normalize_case", "apply_defaults", "select_columns"]
    assert ts[7]["op"] == "deduplicate" and ts[7]["output"] == "dim.customers"
    assert ts[8]["op"] == "aggregate" and ts[8]["keys"] == ["region"] and ts[8]["measures"] == [{"fn": "count", "column": "*", "as": "customers"}]
    assert ts[-1]["primitive"] == "top_n_filter" and ts[-1]["output"] == "/data/mart/top_regions"
    pred = ts[1]["predicate"]
    assert pred["op"] == "and" and pred["left"] == {"e": "unop", "op": "not", "operand": {"e": "unop", "op": "missing", "operand": {"e": "var", "name": "customer_id"}}}


def test_pig_join_split_union_and_platform_operators():
    text = ("a = LOAD '/d/a' USING PigStorage(',') AS (id:int, k:int, v:double);\n"
            "b = LOAD 'db.b' USING org.apache.hive.hcatalog.pig.HCatLoader();\n"
            "j = JOIN a BY k LEFT OUTER, b BY k;\n"
            "SPLIT j INTO big IF v > 100.0, small IF v <= 10.0, mid OTHERWISE;\n"
            "u = UNION ONSCHEMA big, small;\n"
            "c = COGROUP a BY id, b BY id;\n"
            "n = FOREACH c { s = ORDER a BY v; GENERATE group, s; };\n"
            "x = FOREACH mid GENERATE id, (int)v AS v, (v > 50.0 ? 'Y' : 'N') AS hi;\n"
            "STORE u INTO '/out/u/$RUN';\n"
            "STORE x INTO '/out/x';\n")
    out = run("etl/j.pig", text)
    assert_valid(out)
    rels = relations(out)
    me = prog("pig", "etl/j.pig")
    assert (me, "READS_FROM", ds("/d/a")) in rels and (me, "READS_FROM", table("db.b")) in rels
    assert rels[(me, "WRITES_TO", ds("/out/u/{RUN}"))] == {"parameterized": True}
    (unit,) = transforms(out)
    ts = unit["transforms"]
    joins = [t for t in ts if t["op"] == "join"]
    assert joins[0]["join"] == {"type": "left", "on": [["k", "k"]]} and joins[0]["primitive"] == "join"
    splits = [t for t in ts if t.get("primitive") == "split_by_condition"]
    assert len(splits) == 3
    assert splits[2]["predicate"]["op"] == "and"  # OTHERWISE: neither earlier condition holds
    union = next(t for t in ts if t["op"] == "union")
    assert union["primitive"] == "union_by_name" and union["dynamic"] is True
    platform = {t["construct"] for t in ts if t["op"] == "platform"}
    assert platform == {"hdp:pig_cogroup", "hdp:pig_nested_foreach"}
    prims = {t.get("primitive") for t in ts}
    assert {"safe_cast", "conditional_flag"} <= prims
    counts = constructs(out)
    assert counts[("hdp:pig_cogroup", False)] == 1 and counts[("hdp:pig_nested_foreach", False)] == 1


def test_pig_split_without_otherwise_is_a_candidate():
    out = run("etl/s.pig", "a = LOAD '/d/a';\nSPLIT a INTO p IF x > 1, q IF x < 0;\nSTORE p INTO '/o/p';\n")
    ts = transforms(out)[0]["transforms"]
    assert [t.get("candidates") for t in ts if t["op"] == "filter"] == [["split_by_condition"], ["split_by_condition"]]


def test_pig_expressions():
    assert parse_expr("a == 1 AND NOT (b IS NULL)") == {"e": "binop", "op": "and", "left": {"e": "binop", "op": "==", "left": {"e": "var", "name": "a"}, "right": {"e": "lit", "value": 1, "type": "number"}},
                                                         "right": {"e": "unop", "op": "not", "operand": {"e": "unop", "op": "missing", "operand": {"e": "var", "name": "b"}}}}
    assert parse_expr("r::x MATCHES '.*a'")["callee"]["name"] == "MATCHES"
    assert parse_expr("s IN ('a', 'b')")["right"]["e"] == "array"
    assert parse_expr("d == $DAY") == {"e": "binop", "op": "==", "left": {"e": "var", "name": "d"}, "right": {"e": "var", "name": "DAY", "scope": "parameter"}}
    assert parse_expr("a ==") is None


def test_pig_statement_splitting_ignores_quotes_comments_and_blocks():
    text = "a = LOAD '/x;y' AS (s:chararray); -- c;d\n/* e; */ b = FILTER a BY s != ';';\nSTORE b INTO '/z';\n"
    out = run("etl/q.pig", text)
    rels = relations(out)
    me = prog("pig", "etl/q.pig")
    assert (me, "READS_FROM", ds("/x;y")) in rels and (me, "WRITES_TO", ds("/z")) in rels
    assert [t["op"] for t in transforms(out)[0]["transforms"]] == ["read", "filter"]


# ---------------------------------------------------------------------- Sqoop


def test_sqoop_options_file_is_a_program():
    out = run(OPTS, fixture(OPTS))
    ents, rels = entities(out), relations(out)
    p = ents[prog("sqoop", OPTS)]
    assert p == {**p, "platform_kind": "hdp:SqoopJob", "kind": "options_file", "tool": "import", "incremental": "append", "check_column": "product_id", "split_by": "product_id"}
    assert rels[(prog("sqoop", OPTS), "READS_FROM", table("catalog.products"))] == {"via": "sqoop", "engine": "oracle"}
    assert (prog("sqoop", OPTS), "WRITES_TO", table("dim.products")) in rels
    (unit,) = transforms(out)
    pred = unit["transforms"][0]["predicate"]
    assert pred["right"] == {"e": "var", "scope": "sqoop", "name": "last_value"}


def test_sqoop_options_file_without_a_tool_declares_nothing():
    out = run("sqoop/common.opts", "--connect\njdbc:mysql://h/db\n--username\nu\n")
    assert not entities(out) and constructs(out) == {("hdp:sqoop_options_file", False): 1}
    assert "jdbc" not in text_of(out)
    assert run("sqoop/notes.txt", "Remember to rotate the import job.\n").observations == []


def test_sqoop_free_form_query_import():
    path = "sqoop/q.opts"
    out = run(path, "import\n--connect\njdbc:teradata://h/DATABASE=x\n--query\n\"SELECT o.id, c.name FROM sales.o o JOIN sales.c c ON o.cid = c.id WHERE $CONDITIONS\"\n--target-dir\n/data/q\n")
    rels = relations(out)
    me = prog("sqoop", path)
    assert (me, "READS_FROM", table("sales.o")) in rels and (me, "READS_FROM", table("sales.c")) in rels
    assert (me, "WRITES_TO", ds("/data/q")) in rels
    assert constructs(out)[("hdp:sqoop_free_form_query", True)] == 1
    assert_valid(out)


def test_sqoop_import_all_tables_is_not_guessed():
    out = run("sqoop/all.opts", "import-all-tables\n--connect\njdbc:mysql://h/db\n--warehouse-dir\n/w\n")
    assert not relation_obs(out, "READS_FROM") and not relation_obs(out, "WRITES_TO")
    assert "sqoop import-all-tables names its tables only at run time" in messages(out)


# ---------------------------------------------------------------------- Oozie details


def test_workflow_sub_workflow_distcp_fs_and_unknown_transition():
    xml = """<workflow-app name="w" xmlns="uri:oozie:workflow:0.5">
  <start to="copy"/>
  <action name="copy"><distcp xmlns="uri:oozie:distcp-action:0.2"><arg>-update</arg><arg>${nameNode}/data/a</arg><arg>hdfs://nn2:8020/backup/a</arg></distcp>
    <ok to="tidy"/><error to="fail"/></action>
  <action name="tidy"><fs><move source="${nameNode}/tmp/x" target="${nameNode}/data/x"/><delete path="/tmp/y"/></fs>
    <ok to="child"/><error to="fail"/></action>
  <action name="child"><sub-workflow><app-path>${wf:appPath()}/child</app-path><propagate-configuration/></sub-workflow>
    <ok to="custom"/><error to="fail"/></action>
  <action name="custom"><my-action xmlns="uri:custom:0.1"><x>1</x></my-action><ok to="nowhere"/><error to="fail"/></action>
  <kill name="fail"><message>failed</message></kill>
  <end name="end"/>
</workflow-app>
"""
    path = "apps/w/workflow.xml"
    out = run(path, xml)
    assert_valid(out)
    ents, rels = entities(out), relations(out)
    t = lambda n: task(n, path)  # noqa: E731
    assert rels[(t("copy"), "READS_FROM", ds("/data/a"))] == {"via": "distcp"}
    assert (t("copy"), "WRITES_TO", ds("/backup/a")) in rels
    assert (t("tidy"), "READS_FROM", ds("/tmp/x")) in rels and (t("tidy"), "WRITES_TO", ds("/data/x")) in rels
    assert ents[t("tidy")]["operations"] == ["delete", "move"]
    assert (t("child"), "RUNS", job("apps/w/child/workflow.xml")) in rels
    assert ents[t("custom")]["type"] == "my-action"
    counts = constructs(out)
    assert counts[("hdp:oozie_action_other", False)] == 1
    assert counts[("hdp:oozie_transition", False)] == 1
    assert "transition to 'nowhere', which is not a node of this workflow" in messages(out)
    assert "nn2" not in text_of(out)
    shapes = {ref(u["owner"]): u["transforms"] for u in transforms(out)}
    assert shapes[t("copy")][0]["output"] == "/backup/a"


def test_workflow_script_named_at_run_time_is_not_resolved():
    xml = ('<workflow-app name="w" xmlns="uri:oozie:workflow:0.5"><start to="h"/>'
           '<action name="h"><hive xmlns="uri:oozie:hive-action:0.5"><script>${scriptDir}/x.hql</script></hive><ok to="end"/><error to="end"/></action>'
           '<end name="end"/></workflow-app>')
    out = run("apps/w/workflow.xml", xml)
    assert not relation_obs(out, "RUNS")
    assert entities(out)[task("h", "apps/w/workflow.xml")]["script"] == "{scriptDir}/x.hql"
    assert "the script named at run time in <script> is not resolved" in messages(out)


def test_doctype_entities_and_malformed_xml_are_refused():
    bomb = '<?xml version="1.0"?><!DOCTYPE w [<!ENTITY a "aaaa">]><workflow-app name="&a;"><start to="end"/><end name="end"/></workflow-app>'
    out = run("w/workflow.xml", bomb)
    assert not out.parsed and out.observations == []
    assert "an Oozie definition with a DOCTYPE or entity declarations is not read" in messages(out)
    out = run("w/workflow.xml", '<workflow-app name="w"><start to="end"/>')
    assert not out.parsed and out.observations == [] and messages(out)[0].startswith("not well-formed XML")


def test_spans_are_exact():
    data = fixture(WF)
    out = run(WF, data)
    for o in relation_obs(out, "DEPENDS_ON"):
        s = o.evidence.span
        assert data[s.start_byte:s.end_byte].startswith((b"<ok", b"<error")), data[s.start_byte:s.end_byte]
    tasks = [o for o in out.observations if o.kind == "entity" and o.payload["kind"] == "Task"]
    for o in tasks:
        s = o.evidence.span
        chunk = data[s.start_byte:s.end_byte]
        assert chunk.startswith(b"<action") and chunk.endswith(b"</action>")
    data = fixture(LOAD)
    out = run(LOAD, data)
    loc = [o for o in relation_obs(out, "DERIVED_FROM")][0].evidence.span
    assert data[loc.start_byte:loc.end_byte] == b"'${nameNode}/data/raw/orders'"


# ---------------------------------------------------------------------- shared names


def test_dataset_paths_drop_hosts_and_keep_parameters():
    assert dataset_path("${nameNode}/data/raw/orders/") == ("/data/raw/orders", False)
    assert dataset_path("hdfs://nn1.example:8020/data//x") == ("/data/x", False)
    assert dataset_path("s3a://landing/orders/${dt}") == ("s3a://landing/orders/{dt}", True)
    assert dataset_path("/data/$DAY/x", pig=True) == ("/data/{DAY}/x", True)
    assert dataset_path("${inputDir}") is None
    assert dataset_path("orders") is None and dataset_path("-m") is None
    assert dataset_path("user@host:/x") is None
    assert table_name("`Mart`.Orders") == ("mart.orders", False)
    assert table_name("${db}.orders") == ("{db}.orders", True)
    assert resolve_relative("apps/a", "hive/x.hql") == "apps/a/hive/x.hql"
    assert resolve_relative("apps/a", "../../../x") is None and resolve_relative("apps/a", "/abs/x") is None


# ---------------------------------------------------------------------- determinism and layers


@pytest.mark.parametrize("path", ALL_BASE)
def test_output_is_deterministic(path):
    a, b = run(path, fixture(path)), run(path, fixture(path))
    assert [json.dumps(o.payload, sort_keys=True, default=str) for o in a.observations] == [json.dumps(o.payload, sort_keys=True, default=str) for o in b.observations]
    assert messages(a) == messages(b)


def test_fixture_layers():
    customers = "sqoop/import_customers.opts"
    out = run(customers, fixture(customers, "commits/01-customers-import"))
    assert (prog("sqoop", customers), "WRITES_TO", ds("/data/raw/customers")) in relations(out)
    assert "crm.example.internal" not in text_of(out)
    weekly = "apps/daily_orders/weekly_coordinator.xml"
    out = run(weekly, fixture(weekly, "commits/02-weekly-coordinator"))
    assert entities(out)[job(weekly)]["frequency"] == {"every": 7, "unit": "day"}
    assert (job(weekly), "RUNS", job(WF)) in relations(out)
    out = run(WF, fixture(WF, "commits/03-retire-pig"))
    ents, rels = entities(out), relations(out)
    assert task("clean-customers") not in ents
    assert rels[(task("daily-summary"), "DEPENDS_ON", task("prepare-orders"))] == {"on": "ok"}
    assert rels[(task("export-summary"), "DEPENDS_ON", task("daily-summary"))] == {"on": "ok", "via": "decision"}
