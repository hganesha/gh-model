"""Transform shapes (repo_intel.compile.transforms, contract section 8.1): the schema, each
recognised idiom in SQL, SAS and PySpark, partial matches, dynamic names, storage in the
shard, tiers and determinism."""

import copy
import json

import pytest

from conftest import REPO_ID
from repo_intel.compile import compiler
from repo_intel.compile import transforms as tx
from repo_intel.compile.contracts import Entity, Manifest, entity_id
from repo_intel.compile.fixtures import materialize
from repo_intel.compile.ontology import EntityKind
from repo_intel.compile.pipeline import compile_commit, load_shards
from repo_intel.extractors.data_python import DataPython
from repo_intel.extractors.sql_lineage import SqlScripts
from repo_intel.knowledge import behavior_store, bundle
from repo_intel.platforms.sas import SasSource
from repo_intel.storage.store import ObjectStore

FIXTURE = "F24-transform-shapes"


def sql(*statements, dialect=None):
    """Transforms of SQL statements, as one unit."""
    b = tx.Builder()
    at = 0
    for s in statements:
        tx.sql_query(b, s, [at, at + len(s)], dialect=dialect)
        at += len(s) + 1
    out = b.finish()
    assert not out or tx.validate_shape({"owner": "@file", "language": "sql", "transforms": out}) == []
    return out


def sas(text):
    """(kind, language, transforms) for each behaviour unit of a SAS program."""
    extraction = SasSource().extract("p.sas", text.encode(), "0" * 64)
    out = []
    for o in extraction.observations:
        if o.kind == "behavior":
            derived = tx.from_behavior(o.payload)
            if derived is not None:
                assert tx.validate_shape({"owner": o.payload["owner"], "language": derived[0], "transforms": derived[1]}) == []
            out.append((o.payload.get("kind"), *(derived or (None, None))))
    return out


def sas_step(text):
    """The transforms of the only SAS step in `text` that has any."""
    found = [t for _, _, t in sas(text) if t]
    assert len(found) == 1, found
    return found[0]


def spark(text, path="jobs/job.py"):
    """The `transform` observations data_python emits for a Python file."""
    extraction = DataPython().extract(path, text.encode(), "0" * 64)
    return [o.payload for o in extraction.observations if o.kind == "transform"]


def by_primitive(transforms):
    return {t["primitive"]: t for t in transforms if "primitive" in t}


# ------------------------------------------------------------------ the contract


def test_vocabularies_match_the_contract():
    assert tx.SCHEMA == "rkp-transform/1"
    assert tx.OPS == ("read", "project", "filter", "join", "union", "deduplicate", "aggregate", "window", "pivot", "unpivot", "sort", "validate", "publish", "platform")
    assert set(tx.PRIMITIVES.values()) <= set(tx.CATEGORIES)
    assert tx.CATEGORIES == ("structural_reshaping", "type_standardization", "cleaning", "deduplication", "cdc", "integration", "business_rules", "aggregation")
    assert len(tx.PRIMITIVES) == 27
    for name in ("calculate_score", "classify_record", "derive_status"):
        assert name not in tx.PRIMITIVES, "business-meaning names are for rule lifting"


def _unit(**over):
    t = {"id": "t1", "op": "deduplicate", "at": [120, 410], "inputs": ["work.orders_sorted"], "output": "work.orders_latest", "keys": ["order_id"], "order": [{"column": "updated_at", "desc": True}], "primitive": "dedupe_by_key"}
    t.update(over)
    return {"owner": {"kind": "Program", "natural_key": {"platform": "sas", "name": "p.sas"}, "name": "p.sas"}, "language": "sas", "transforms": [t]}


def test_the_contract_example_validates():
    assert tx.validate_shape(_unit()) == []


@pytest.mark.parametrize("change, problem", [
    ({"id": "t2"}, "id must be 't1'"),
    ({"op": "explode"}, "unknown op"),
    ({"at": [5, 1]}, "needs a span"),
    ({"primitive": "calculate_score"}, "unknown primitive"),
    ({"candidates": ["join"]}, "never both"),
    ({"order": [{"column": "x"}]}, "order[0]"),
    ({"join": {"type": "outer"}}, "join must be"),
    ({"columns": {"keep": ["a"]}}, "columns may hold only"),
    ({"dynamic": False}, "dynamic is present only when true"),
    ({"predicate": {"e": "nope"}}, "not an expression"),
    ({"colour": "red"}, "unknown field"),
    ({"inputs": ["t1"]}, "cannot read its own result"),
])
def test_validate_shape_names_every_break(change, problem):
    problems = tx.validate_shape(_unit(**change))
    assert any(problem in p for p in problems), problems


def test_a_platform_transform_names_its_construct_and_owners_may_be_strings():
    unit = {"owner": "@package", "language": "pyspark", "transforms": [{"id": "t1", "op": "platform", "at": [0, 1], "inputs": []}]}
    assert any("names its construct" in p for p in tx.validate_shape(unit))
    unit["transforms"][0]["construct"] = "pyspark:foreach"
    assert tx.validate_shape(unit) == []
    assert tx.validate_shape({**unit, "owner": "@nobody"})
    assert tx.validate_shape({**unit, "language": "fortran"})
    assert tx.validate_shape({**unit, "transforms": []})


def test_transform_observation_validates_before_it_is_emitted():
    from repo_intel.extractors.base import span

    good = _unit()
    o = tx.transform_observation("0" * 64, span(b"x" * 500, 0, 500), good["owner"], "sas", good["transforms"])
    assert o.kind == "transform" and o.payload["schema"] == tx.SCHEMA and o.payload["transforms"] == good["transforms"]
    with pytest.raises(ValueError):
        tx.transform_observation("0" * 64, span(b"x", 0, 1), good["owner"], "sas", [{"id": "t9", "op": "read", "at": [0, 1], "inputs": []}])


def test_purpose_is_the_category_with_most_primitives_ties_by_category_order():
    names, counts = tx.summary([{"primitive": "aggregate"}, {"primitive": "aggregate"}, {"primitive": "trim_strings"}, {"op": "platform"}])
    assert names == ["aggregate", "trim_strings"] and tx.purpose(counts) == "aggregation"
    _, counts = tx.summary([{"primitive": "merge_into"}, {"primitive": "join"}])
    assert tx.purpose(counts) == "cdc", "cdc comes before integration"
    assert tx.purpose({}) is None


# ------------------------------------------------------------------ SQL


def test_row_number_equals_one_is_dedupe_by_key_in_a_subquery_or_qualify():
    for statement in (
        "insert into stg.latest select * from (select o.*, row_number() over (partition by order_id order by updated_at desc) as rn from raw.orders o) x where rn = 1",
        "create table stg.latest as select * from raw.orders qualify row_number() over (partition by order_id order by updated_at desc) = 1",
    ):
        [t] = sql(statement)
        assert t == {"id": "t1", "op": "deduplicate", "at": t["at"], "inputs": ["raw.orders"], "output": "stg.latest", "keys": ["order_id"], "order": [{"column": "updated_at", "desc": True}], "primitive": "dedupe_by_key"}


def test_rank_and_top_n_are_not_dedupe():
    [t] = sql("select * from (select *, row_number() over (partition by k order by ts desc) as rn from t) x where rn <= 3")
    assert t["op"] == "filter" and t["primitive"] == "top_n_filter"
    [t] = sql("select * from (select *, rank() over (partition by k order by ts desc) as rn from t) x where rn = 1")
    assert "primitive" not in t and t["candidates"] == ["dedupe_by_key"], "rank keeps ties: a partial match"
    [t] = sql("select * from t order by amount desc limit 10")
    assert t["primitive"] == "top_n_filter" and t["order"] == [{"column": "amount", "desc": True}]


def test_group_by_is_aggregate_and_a_computed_aggregate_is_a_candidate():
    [t] = sql("insert into mart.daily select region, sum(amt) as total, count(*) as n from stg.orders group by region")
    assert t["primitive"] == "aggregate" and t["keys"] == ["region"] and t["output"] == "mart.daily"
    assert t["measures"] == [{"fn": "sum", "column": "amt", "as": "total"}, {"fn": "count", "column": "*", "as": "n"}]
    [t] = sql("select region, sum(amt) / count(*) as avg_amt from t group by region")
    assert "primitive" not in t and t["candidates"] == ["aggregate"]


def test_joins_anti_semi_and_not_in():
    [t] = sql("select a.* from a left join b on a.k = b.k where b.k is null")
    assert t["join"] == {"type": "anti", "on": [["k", "k"]]} and t["primitive"] == "join"
    [t] = sql("select * from a where not exists (select 1 from b where b.k = a.k)")
    assert t["join"]["type"] == "anti" and t["primitive"] == "join"
    [t] = sql("select * from a where exists (select 1 from b where b.k = a.k)")
    assert t["join"]["type"] == "semi"
    t = sql("select * from a where k not in (select k from b)")[-1]
    assert t["join"]["type"] == "anti" and t["candidates"] == ["join"], "NOT IN differs from an anti join on NULL"
    [t] = sql("select * from a join b on a.k = b.k and a.d between b.f and b.t")
    assert t["candidates"] == ["join"] and "sql" in t["predicate"]


def test_merge_into_apply_deletes_and_partial_merges():
    [t] = sql("merge into dim.c t using stg.c s on t.id = s.id when matched then update set name = s.name when not matched then insert (id, name) values (s.id, s.name)")
    assert t["op"] == "publish" and t["primitive"] == "merge_into" and t["keys"] == ["id"] and t["output"] == "dim.c" and t["inputs"] == ["stg.c"]
    [t] = sql("merge into dim.c t using gone s on t.id = s.id when matched then delete")
    assert t["primitive"] == "apply_deletes"
    [t] = sql("merge into dim.c t using stg.c s on t.id = s.id when matched then update set name = s.name")
    assert t["candidates"] == ["merge_into"]


def test_an_end_dating_update_then_an_insert_is_scd_type_2():
    update = "update dim.c set valid_to = current_date, is_current = 0 where id in (select id from stg.c)"
    insert = "insert into dim.c (id, name, valid_from, is_current) select id, name, current_date, 1 from stg.c"
    out = sql(update, insert)
    assert out[0]["primitive"] == "apply_scd_type_2" and out[0]["columns"] == {"replace": ["is_current", "valid_to"]}
    [alone] = sql(update)
    assert "primitive" not in alone and "apply_scd_type_2" in alone["candidates"], "without the insert it is only a candidate"
    assert "apply_scd_type_2" not in json.dumps(sql("update t set name = upper(name)"))


def test_the_select_list_column_effects():
    out = sql("select trim(name) as name, upper(code) as code, coalesce(amt, 0) as amt, coalesce(a, b) as ab, try_cast(x as int) as x, cast(y as int) as y, "
              "to_date(d) as d, case status when 'A' then 'Active' when 'I' then 'Inactive' end as status_desc, "
              "case when amt > 0 then 1 else 0 end as has_amt, amt * 2 as dbl, id from t")
    got = {t.get("primitive") or tuple(t.get("candidates", [])): t.get("columns") for t in out}
    assert got["trim_strings"] == {"replace": ["name"]} and got["normalize_case"] == {"replace": ["code"]}
    assert got["apply_defaults"] == {"replace": ["amt"]} and got["coalesce_columns"] == {"add": ["ab"]}
    assert got["safe_cast"] == {"replace": ["x"]} and got[("safe_cast",)] == {"replace": ["y"]}, "a plain CAST may fail: a candidate"
    assert got["parse_timestamp"] == {"replace": ["d"]} and got["map_codes"] == {"add": ["status_desc"]}
    assert got["conditional_flag"] == {"add": ["has_amt"]} and got["derive_column"] == {"add": ["dbl"]}
    assert out[-1]["primitive"] == "select_columns" and "columns" not in out[-1]
    [rename] = [t for t in sql("select a as b, c from t") if t.get("primitive") == "rename_columns"]
    assert rename["columns"] == {"rename": {"a": "b"}}


def test_unions_pivots_and_distinct():
    out = sql("select a, b from x union by name select a, b from y", dialect="duckdb")
    assert any(t.get("primitive") == "union_by_name" for t in out)
    out = sql("select a, b from x union select a, b from y")
    assert out[-1]["op"] == "deduplicate" and out[-1]["candidates"] == ["dedupe_by_hash"]
    assert "primitive" not in next(t for t in out if t["op"] == "union"), "a positional union is not by name"
    [t] = sql("select * from t pivot (sum(amt) for month in ('jan', 'feb'))", dialect="snowflake")
    assert t["op"] == "pivot" and t["primitive"] == "pivot"
    [t] = sql("select * from t unpivot (amt for month in (jan, feb))", dialect="snowflake")
    assert t["op"] == "unpivot" and t["primitive"] == "unpivot"
    t = sql("select distinct a, b from t")[-1]
    assert t["op"] == "deduplicate" and t["candidates"] == ["dedupe_by_hash"]


def test_windows_running_totals_and_deltas():
    out = sql("select k, sum(amt) over (partition by k order by ts rows between unbounded preceding and current row) as run from t")
    assert out[0]["primitive"] == "calculate_running_total" and out[0]["keys"] == ["k"]
    out = sql("select k, amt - lag(amt) over (partition by k order by ts) as d from t")
    assert out[0]["primitive"] == "delta_calc" and out[0]["measures"] == [{"fn": "lag", "column": "amt", "as": "d"}]
    out = sql("select k, avg(amt) over (partition by k) as a from t")
    assert out[0]["primitive"] == "window_function"


def test_deletes_ddl_and_text_that_does_not_parse():
    [t] = sql("delete from t where id in (select id from gone)")
    assert t["op"] == "filter" and t["candidates"] == ["apply_deletes"] and t["inputs"] == ["t", "gone"] and t["predicate"]["sql"].startswith("NOT (")
    assert sql("create table t (id int)") == [], "DDL moves no data"
    [t] = sql("truncate table stg.x")
    assert t["op"] == "platform" and t["construct"] == "sql:truncate"
    [t] = sql("insert into t select from where")
    assert t["op"] == "platform" and t["construct"].startswith("sql:unparsed")


def test_dynamic_and_remote_sql_is_one_platform_transform():
    [t] = sql("insert into stg.orders_#region# select * from raw.orders")
    assert t == {"id": "t1", "op": "platform", "at": t["at"], "inputs": ["raw.orders"], "output": "stg.orders_{region}", "dynamic": True, "construct": "sql:dynamic_table"}
    b = tx.Builder()
    tx.sql_query(b, "select * from orders", [0, 1], remote="oracle")
    assert b.finish() == [{"id": "t1", "op": "platform", "at": [0, 1], "inputs": [], "dynamic": True, "construct": "sql:remote:oracle"}]


def test_sql_scripts_emit_one_observation_per_program():
    text = ("create procedure dbo.load_dim as\nbegin\n"
            "  update dim.c set valid_to = getdate(), is_current = 0 from dim.c join stg.c s on s.id = dim.c.id where dim.c.is_current = 1;\n"
            "  insert into dim.c (id, name, valid_from, is_current) select id, name, getdate(), 1 from stg.c;\nend;\n")
    extraction = SqlScripts().extract("sql/load.sql", text.encode(), "0" * 64)
    [o] = [o for o in extraction.observations if o.kind == "transform"]
    assert o.payload["owner"]["natural_key"] == {"platform": "tsql", "name": "dbo.load_dim"} and o.payload["language"] == "sql"
    first = o.payload["transforms"][0]
    assert first["primitive"] == "apply_scd_type_2" and first["inputs"] == ["dim.c", "stg.c"]
    assert not [o for o in SqlScripts().extract("r.sql", b"select * from t;", "0" * 64).observations if o.kind == "transform"], "a report query moves no data"


# ------------------------------------------------------------------ SAS


def test_proc_sort_then_first_dot_is_dedupe_by_key():
    out = sas("proc sort data=raw.orders out=work.sorted; by order_id descending updated_at; run;\n"
              "data work.latest; set work.sorted; by order_id descending updated_at; if first.order_id; run;\n")
    sort, data = out[0][2], out[1][2]
    assert sort == [{"id": "t1", "op": "sort", "at": sort[0]["at"], "inputs": ["raw.orders"], "output": "work.sorted", "order": [{"column": "order_id", "desc": False}, {"column": "updated_at", "desc": True}]}]
    [t] = data
    assert t["op"] == "deduplicate" and t["primitive"] == "dedupe_by_key" and t["keys"] == ["order_id"] and t["order"] == [{"column": "updated_at", "desc": True}]
    [t] = sas_step("data work.oldest; set work.sorted; by order_id descending updated_at; if last.order_id; run;")
    assert t["order"] == [{"column": "updated_at", "desc": False}], "LAST. keeps the other end of the BY order"


def test_proc_sort_nodupkey_and_nodup():
    out = sas_step("proc sort data=work.a nodupkey; by k; run;")
    assert out[-1]["primitive"] == "dedupe_by_key" and out[-1]["output"] == "work.a"
    out = sas_step("proc sort data=work.a out=work.b noduprecs; by k; run;")
    assert out[-1]["candidates"] == ["dedupe_by_hash"]


@pytest.mark.parametrize("condition, join_type, swapped", [
    ("if a and not b;", "anti", False),
    ("if b and not a;", "anti", True),
    ("if a and b;", "inner", False),
    ("if a;", "left", False),
    ("if b;", "right", False),
    ("if a or b;", "full", False),
])
def test_merge_in_flags_give_the_join_type(condition, join_type, swapped):
    [t] = sas_step(f"data work.out; merge work.cur(in=a) work.prev(in=b); by id; {condition} run;")
    assert t["op"] == "join" and t["join"] == {"type": join_type, "on": [["id", "id"]]} and t["primitive"] == "join"
    assert t["inputs"] == (["work.prev", "work.cur"] if swapped else ["work.cur", "work.prev"])


def test_merge_without_by_or_with_other_conditions_is_not_a_join_primitive():
    [t] = sas_step("data work.out; merge work.a work.b; run;")
    assert t["op"] == "platform" and t["construct"] == "sas:merge_positional"
    # A condition that reads more than the IN= flags: the MERGE stays a full join and the IF a filter.
    join, kept = sas_step("data work.out; merge work.a(in=a) work.b(in=b); by id; if a and amount > 0; run;")
    assert join["join"]["type"] == "full" and kept["op"] == "filter" and kept["inputs"] == ["t1"] and kept["output"] == "work.out"


def test_retain_with_first_and_last_is_aggregate_and_without_last_a_running_total():
    out = sas_step("data mart.totals(keep=cust total); set work.txn; by cust; retain total; if first.cust then total = 0; total + amt; if last.cust then output; run;")
    assert out[0] == {**out[0], "op": "aggregate", "keys": ["cust"], "measures": [{"fn": "sum", "column": "amt", "as": "total"}], "primitive": "aggregate"}
    assert out[-1]["output"] == "mart.totals"
    [t] = sas_step("data work.running; set work.txn; by cust; if first.cust then bal = 0; bal + amt; run;")
    assert t["op"] == "window" and t["primitive"] == "calculate_running_total" and t["keys"] == ["cust"]
    [t] = sas_step("data work.n; set work.txn; by cust; if first.cust then n = 0; n + 1; if last.cust then output; run;")
    assert t["measures"] == [{"fn": "count", "as": "n"}] and t["primitive"] == "aggregate"


def test_proc_transpose_means_freq_and_append():
    [t] = sas_step("proc transpose data=work.long out=work.wide prefix=m_; by id; id month; var value; run;")
    assert t["op"] == "pivot" and t["primitive"] == "pivot" and t["keys"] == ["id"]
    [t] = sas_step("proc transpose data=work.wide out=work.long; by id; var m1 m2; run;")
    assert t["op"] == "unpivot" and t["primitive"] == "unpivot" and t["columns"] == {"drop": ["m1", "m2"]}
    [t] = sas_step("proc means data=work.txn nway noprint; class cust; var amt; output out=work.s sum=total mean=avg; run;")
    assert t["primitive"] == "aggregate" and t["keys"] == ["cust"] and t["measures"] == [{"fn": "sum", "column": "amt", "as": "total"}, {"fn": "mean", "column": "amt", "as": "avg"}]
    [t] = sas_step("proc means data=work.txn noprint; class cust; var amt; output out=work.s sum=total; run;")
    assert t["candidates"] == ["aggregate"], "without NWAY the output holds every level of the CLASS ways"
    [t] = sas_step("proc summary data=work.txn nway; class cust; var amt; output out=work.s sum= / autoname; run;")
    assert t["primitive"] == "aggregate" and t["measures"] == [{"fn": "sum", "column": "amt", "as": "amt_sum"}]
    [t] = sas_step("proc freq data=work.txn noprint; tables region / out=work.f; run;")
    assert t["op"] == "aggregate"
    [t] = sas_step("proc append base=mart.all data=work.new; run;")
    assert t["primitive"] == "union_by_name" and t["output"] == "mart.all"


def test_reports_and_data_null_move_no_data():
    steps = lambda text: [u for u in sas(text) if u[0] != "open_code"]  # noqa: E731
    assert steps("proc print data=work.a; run;") == [("proc_step", None, None)]
    assert steps("data _null_; set work.a; put _all_; run;") == [("data_step", None, None)]


def test_data_step_assignments_options_and_splits():
    out = sas_step("data work.clean; set work.raw(rename=(old=new) where=(amt > 0)); name = upcase(name); code = strip(code); "
                   "reg = put(region, $region.); dt = input(dtxt, yymmdd10.); n = input(ntxt, best12.); "
                   "if amt > 0 then pos = 'Y'; else pos = 'N'; if missing(qty) then qty = 0; d = amt - lag(amt); drop dtxt; run;")
    prims = by_primitive(out)
    assert out[0]["op"] == "filter" and out[0]["predicate"] == {"sql": "amt > 0"}
    assert prims["rename_columns"]["columns"] == {"rename": {"old": "new"}}
    assert prims["normalize_case"]["columns"] == {"replace": ["name"]} and prims["trim_strings"]["columns"] == {"replace": ["code"]}
    assert prims["map_codes"]["columns"] == {"add": ["reg"]} and prims["parse_timestamp"]["columns"] == {"add": ["dt"]}
    assert prims["conditional_flag"]["columns"] == {"add": ["pos"]} and prims["apply_defaults"]["columns"] == {"replace": ["qty"]}
    assert prims["delta_calc"]["measures"] == [{"fn": "lag", "column": "amt", "as": "d"}]
    assert prims["drop_columns"]["columns"] == {"drop": ["dtxt"]} and out[-1]["output"] == "work.clean"
    assert any(t.get("candidates") == ["safe_cast"] for t in out), "a numeric informat may fail: a candidate"
    assert "map_codes" not in by_primitive(sas_step("data work.x; set work.y; s = put(amt, comma12.); run;")), "a built-in format is not a code list"
    split = sas_step("data work.hi work.lo; set work.raw; if amt > 100 then output work.hi; else output work.lo; run;")
    assert [(t["output"], t.get("primitive")) for t in split] == [("work.hi", "split_by_condition"), ("work.lo", "split_by_condition")]


def test_sas_macro_names_stay_dynamic():
    out = sas("%macro slice(region);\ndata mart.orders_&region; set raw.orders_&region; where amount > 0; run;\n"
              "proc sort data=raw.orders_&region out=mart.sorted_&region nodupkey; by order_id; run;\n%mend;\n"
              "proc sql; create table mart.x_&region as select * from raw.y; quit;\n")
    found = [t for _, _, t in out if t]
    assert found[0] == [{**found[0][0], "inputs": ["raw.orders_{region}"], "output": "mart.orders_{region}", "dynamic": True}]
    assert found[1][-1]["output"] == "mart.sorted_{region}" and all(t["dynamic"] for t in found[1])
    [t] = found[2]
    assert t["op"] == "platform" and t["dynamic"] is True and t["output"] == "mart.x_{region}", "a dynamic SQL target is not resolved"


def test_proc_sql_is_read_with_the_sql_recognizer():
    out = sas("proc sql; create table work.agg as select region, sum(calculated amt) as total from work.txn group by region; quit;")
    [(kind, language, [t])] = [x for x in out if x[2]]
    assert kind == "sql_step" and language == "sql" and t["primitive"] == "aggregate" and t["inputs"] == ["work.txn"]
    out = sas("proc sql; create table agg as select region, count(*) as n from txn group by region; quit;")
    assert [x for x in out if x[2]][0][2][0]["inputs"] == ["work.txn"], "a one-level name is WORK"
    out = sas('proc sql; insert into rpt.audit_log (table_name, loaded_at) values ("x", datetime()); quit;')
    [t] = [x for x in out if x[2]][0][2]
    assert {k: v for k, v in t.items() if k != "at"} == {"id": "t1", "op": "publish", "inputs": [], "output": "rpt.audit_log"}, "SAS DATETIME() reads as SQL"


# ------------------------------------------------------------------ PySpark


SPARK_HEADER = "from pyspark.sql import Window\nimport pyspark.sql.functions as F\n"


def test_pyspark_row_number_filter_is_dedupe_by_key():
    [u] = spark(SPARK_HEADER + "w = Window.partitionBy('order_id').orderBy(F.col('updated_at').desc())\n"
                "spark.read.table('raw.orders').withColumn('rn', F.row_number().over(w)).filter(F.col('rn') == 1).drop('rn').write.saveAsTable('stg.latest')\n")
    assert u["owner"] == "@file" and u["language"] == "pyspark", "outside a notebook the shapes belong to the file, not its package"
    first, last = u["transforms"][0], u["transforms"][-1]
    assert first["primitive"] == "dedupe_by_key" and first["keys"] == ["order_id"] and first["order"] == [{"column": "updated_at", "desc": True}]
    assert last["primitive"] == "drop_columns" and last["output"] == "stg.latest"


def test_pyspark_methods():
    [u] = spark(SPARK_HEADER +
        "df = spark.table('raw.orders')\n"
        "c = spark.table('raw.customers')\n"
        "j = df.join(c, df.customer_id == c.id, 'left_anti')\n"
        "g = df.groupBy('region').agg(F.sum('amt').alias('total'))\n"
        "p = df.groupBy('cust').pivot('month').sum('amt')\n"
        "u = df.union(c)\n"
        "s = df.select('a', F.col('b').alias('bb'), F.upper('c').alias('c'))\n"
        "x = df.dropDuplicates(['k']).orderBy('ts').limit(5)\n"
        "y = df.withColumn('h', F.sha2(F.concat_ws('|', 'a', 'b'), 256)).dropDuplicates(['h'])\n"
        "z = df.distinct().fillna(0, subset=['amt'])\n"
        "m = df.unpivot(['id'], ['jan', 'feb'], 'month', 'amt')\n"
        "e = df.selectExpr('trim(name) as name', 'amt')\n"
        "r = df.foo()\n")
    t = u["transforms"]
    get = lambda op, **kw: [x for x in t if x["op"] == op and all(x.get(k) == v for k, v in kw.items())]
    assert get("join", primitive="join")[0]["join"] == {"type": "anti", "on": [["customer_id", "id"]]}
    assert get("aggregate", primitive="aggregate")[0]["measures"] == [{"fn": "sum", "column": "amt", "as": "total"}]
    assert get("pivot", primitive="pivot")[0]["keys"] == ["cust", "month"]
    assert get("union") and "primitive" not in get("union")[0], "union is positional"
    assert get("project", primitive="rename_columns")[0]["columns"] == {"rename": {"b": "bb"}}
    assert get("project", primitive="normalize_case")[0]["columns"] == {"replace": ["c"]}
    assert get("filter", primitive="top_n_filter")[0]["order"] == [{"column": "ts", "desc": False}]
    assert get("deduplicate", primitive="dedupe_by_hash")[0]["keys"] == ["h"]
    assert get("deduplicate", candidates=["dedupe_by_hash"])
    assert get("deduplicate", keys=["k"], candidates=["dedupe_by_key"]), "dropDuplicates has no order: a candidate, not the primitive"
    assert not get("deduplicate", primitive="dedupe_by_key")
    assert get("project", primitive="apply_defaults")[0]["columns"] == {"replace": ["amt"]}
    assert get("unpivot", primitive="unpivot")[0]["keys"] == ["id"]
    assert get("project", primitive="trim_strings")[0]["columns"] == {"replace": ["name"]}
    assert get("platform", construct="pyspark:foo")


def test_pyspark_running_totals_deltas_union_by_name_and_merge():
    [u] = spark(SPARK_HEADER + "from delta.tables import DeltaTable\n"
                "w = Window.partitionBy('k').orderBy('ts')\n"
                "df = spark.table('t')\n"
                "a = df.withColumn('run', F.sum('amt').over(w))\n"
                "b = df.withColumn('d', F.col('amt') - F.lag('amt').over(w))\n"
                "c = df.unionByName(spark.table('t2'))\n"
                "DeltaTable.forName(spark, 'dim.c').alias('t').merge(df.alias('s'), 't.id = s.id').whenMatchedUpdateAll().whenNotMatchedInsertAll().execute()\n"
                "DeltaTable.forName(spark, 'dim.c').alias('t').merge(df.alias('s'), 't.id = s.id').whenMatchedDelete().execute()\n")
    prims = [x.get("primitive") for x in u["transforms"]]
    assert prims == ["calculate_running_total", "delta_calc", "union_by_name", "merge_into", "apply_deletes"]
    merge = u["transforms"][3]
    assert merge["output"] == "dim.c" and merge["keys"] == ["id"] and merge["inputs"] == ["t"]


def test_pyspark_parameters_and_f_strings_stay_dynamic():
    [u] = spark(SPARK_HEADER + "def load(df, region):\n    df.withColumn('r', F.lit(region)).write.saveAsTable(f'stg.orders_{region}')\n")
    t = u["transforms"]
    assert t[0]["inputs"] == ["{df}"] and t[0]["dynamic"] is True and t[-1]["output"] == "stg.orders_{region}"
    [u] = spark(SPARK_HEADER + "def read(name):\n    return spark.read.table(name).filter('amt > 0')\n")
    assert u["transforms"][0]["op"] == "platform" and u["transforms"][0]["dynamic"] is True, "a table named by a parameter is not guessed"


def test_pyspark_modules_own_their_shapes_and_their_package_keeps_the_lineage():
    text = SPARK_HEADER + "spark.table('raw.a').write.saveAsTable('stg.b')\ndef f():\n    spark.table('raw.c').write.saveAsTable('stg.d')\n"
    extraction = DataPython().extract("jobs/job.py", text.encode(), "0" * 64)
    units = [o.payload for o in extraction.observations if o.kind == "transform"]
    assert len(units) == 2 and {u["owner"] for u in units} == {"@file"}, "module and function units belong to the file"
    lineage = [o.payload for o in extraction.observations if o.kind == "relation" and o.payload["predicate"] in ("READS_FROM", "WRITES_TO")]
    assert lineage and {r["subject"] for r in lineage} == {"@package"}


def test_a_pyspark_recognizer_fault_leaves_the_lineage_and_names_only_its_type(monkeypatch):
    from repo_intel.extractors import data_python

    def broken(*args, **kwargs):
        raise KeyError("secret-looking text")

    monkeypatch.setattr(data_python, "pyspark_shapes", broken)
    extraction = DataPython().extract("jobs/job.py", (SPARK_HEADER + "spark.table('raw.a').write.saveAsTable('stg.b')\n").encode(), "0" * 64)
    assert not [o for o in extraction.observations if o.kind == "transform"]
    assert [o for o in extraction.observations if o.kind == "relation"], "the lineage stands"
    assert [d.message for d in extraction.diagnostics] == ["transform shapes not derived: KeyError"]


def test_pandas_and_plain_python_are_never_read_as_spark():
    assert spark("import pandas as pd\ndf = pd.read_csv('a.csv')\ndf = df.drop(columns=['x']).merge(other, on='k')\ndf.to_csv('b.csv')\n") == []
    assert spark("import pyspark\nf = open('x')\nf.write('a')\nrows = sorted(x).filter(y)\n") == []


def test_spark_sql_and_temp_views_continue_the_chain():
    [u] = spark(SPARK_HEADER + "df = spark.table('raw.orders').filter(F.col('amt') > 0)\ndf.createOrReplaceTempView('v')\n"
                "spark.sql('insert into mart.n select region, count(*) as n from v group by region')\n")
    t = u["transforms"]
    assert t[0]["predicate"] == {"e": "binop", "op": ">", "left": {"e": "var", "name": "amt"}, "right": {"e": "lit", "value": 0, "type": "integer"}}
    assert t[1]["op"] == "aggregate" and t[1]["inputs"] == ["t1"] and t[1]["output"] == "mart.n"


def test_notebook_sql_cells_are_sql_units_owned_by_the_notebook():
    text = ("# Databricks notebook source\nx = spark.table('a')\nx.write.saveAsTable('b')\n\n# COMMAND ----------\n\n"
            "# MAGIC %sql\n# MAGIC insert overwrite c select k, sum(v) as s from b group by k\n")
    units = spark(text, "nb/n.py")
    assert [u["language"] for u in units] == ["pyspark", "sql"]
    assert all(u["owner"]["kind"] == "Notebook" for u in units)
    assert units[1]["transforms"][0]["primitive"] == "aggregate"


# ------------------------------------------------------------------ storage, tiers, coverage


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    root = tmp_path_factory.mktemp("f24")
    shas = materialize(FIXTURE, root / "repo")
    store = ObjectStore(root / "store", "t")
    manifests = [compile_commit(store, root / "repo", REPO_ID, sha).manifest for sha in shas]
    return store, manifests, root, shas


def _owner(kind, key):
    return entity_id(REPO_ID, kind, key)


def test_rows_are_stored_sorted_with_the_contract_shape(compiled):
    store, manifests, _, _ = compiled
    shards = load_shards(store, manifests[0])
    rows = [r for s in shards.values() for r in s.get("transforms", [])]
    assert rows and all(set(r) == {"owner", "path", "at", "language", "transforms"} for r in rows)
    for s in shards.values():
        keys = [(r["owner"], r["path"], r["at"][0], r["at"][1]) for r in s.get("transforms", [])]
        assert keys == sorted(keys)
        assert all(tx.validate_shape({"owner": "@file", "language": r["language"], "transforms": r["transforms"]}) == [] for r in s.get("transforms", []))
    sas_rows = [r for r in rows if r["owner"] == _owner("Program", {"platform": "sas", "name": "sas/programs/monthly_orders.sas"})]
    assert len(sas_rows) == 6 and {r["language"] for r in sas_rows} == {"sas"}


def test_owner_attributes_and_coverage(compiled):
    store, manifests, _, _ = compiled
    shards = load_shards(store, manifests[1])
    entities = {e["id"]: e for s in shards.values() for e in s["entities"]}
    history = entities[_owner("Program", {"platform": "sql", "name": "dim.load_customer_history"})]
    assert "apply_scd_type_2" in history["attributes"]["transforms"]["primitives"]
    coverage = [c for c in store.get_json(manifests[0].coverage_ref) if c["domain"] == "transform_shapes"]
    assert coverage == [{"domain": "transform_shapes", "scope": "@root", "numerator": 11, "denominator": 11, "detail": {"by_language": {"pyspark": 3, "sas": 6, "sql": 2}, "statements": {"recognised": 24, "total": 24}}}]


def test_statement_coverage_shows_one_recognised_statement_out_of_ten():
    script = "insert into mart.n select region, count(*) as n from raw.orders group by region;\n" + "".join(f"delete from stg.t{i};\n" for i in range(9))
    extraction = SqlScripts().extract("sql/load.sql", script.encode(), "0" * 64)
    [unit] = [o.payload for o in extraction.observations if o.kind == "transform"]
    row = {"owner": "ent_x", "path": "sql/load.sql", "at": [0, len(script)], "language": "sql", "transforms": unit["transforms"]}
    [c] = compiler.transform_coverage({"@root": {"transforms": [row], "unknowns": []}})
    assert (c["numerator"], c["denominator"]) == (1, 1), "the unit moves data and something in it is recognised"
    assert c["detail"] == {"by_language": {"sql": 1}, "statements": {"recognised": 1, "total": 10}}


def test_a_sql_recognizer_fault_leaves_the_lineage_and_names_only_its_type(monkeypatch):
    def broken(*args, **kwargs):
        raise KeyError("secret-looking text")

    monkeypatch.setattr(tx, "sql_unit", broken)
    extraction = SqlScripts().extract("sql/load.sql", b"insert into mart.n select k, sum(v) as s from raw.b group by k;\n", "0" * 64)
    assert not [o for o in extraction.observations if o.kind == "transform"]
    assert [o for o in extraction.observations if o.kind == "relation"], "the lineage stands"
    assert [d.message for d in extraction.diagnostics] == ["transform shapes not derived: KeyError"]


def test_a_fault_deriving_shapes_from_behaviour_is_an_unknown_and_the_compile_goes_on(tmp_path, monkeypatch):
    def broken(unit):
        raise KeyError("secret-looking text")

    from repo_intel.compile import buildgraph

    monkeypatch.setattr(tx, "from_behavior", broken)
    monkeypatch.setenv(buildgraph.EXECUTOR_ENV, "inprocess")  # the patch exists only in this process
    shas = materialize(FIXTURE, tmp_path / "repo")
    store = ObjectStore(tmp_path / "store", "t")
    manifest = compile_commit(store, tmp_path / "repo", REPO_ID, shas[0]).manifest
    shards = load_shards(store, manifest)
    unknowns = [u for s in shards.values() for u in s["unknowns"] if u["predicate"] == compiler.TRANSFORM_OWNER]
    stored = [b for s in shards.values() for b in s.get("behaviors", [])]
    assert len(stored) == 7 and len(unknowns) == 7, "one per stored SAS step: whether it moves data is not known"
    assert all(u["reason"].startswith("transform shapes not derived at bytes ") and u["reason"].endswith(": KeyError") for u in unknowns)
    assert all("secret" not in u["reason"] for u in unknowns), "the exception's text is never kept"
    rows = [r for s in shards.values() for r in s.get("transforms", [])]
    assert {r["language"] for r in rows} == {"pyspark", "sql"}, "shapes the extractors emitted are still stored"
    [c] = [c for c in store.get_json(manifest.coverage_ref) if c["domain"] == "transform_shapes"]
    assert (c["numerator"], c["denominator"]) == (5, 12)


def test_an_owner_nothing_declares_is_an_unknown_counted_in_coverage():
    from repo_intel.extractors.base import obs, span

    shard = compiler._Shard("@root")
    art = shard.entity(Entity.make(REPO_ID, EntityKind.ARTIFACT, {"path": "x.sql"}, "x.sql"))
    ghost = {"kind": "Program", "natural_key": {"platform": "sql", "name": "ghost"}, "name": "ghost"}
    transforms = [{"id": "t1", "op": "read", "at": [0, 3], "inputs": ["a"], "output": "b"}]
    o = obs("transform", "0" * 64, span(b"abc", 0, 3), {"schema": tx.SCHEMA, "owner": ghost, "language": "sql", "transforms": transforms}).model_dump(mode="json")
    package = {"schema": tx.SCHEMA, "owner": "@package", "language": "sql", "transforms": transforms}
    p = obs("transform", "0" * 64, span(b"abc", 0, 3), package).model_dump(mode="json")
    compiler._transforms(shard, REPO_ID, "", None, {"x.sql": (art, "ev")}, [{"path": "x.sql", "observations": [o, p]}])
    rows = shard.rows()
    [unknown] = [u for u in rows["unknowns"] if u["predicate"] == compiler.TRANSFORM_OWNER]
    assert "ghost" in unknown["reason"] and unknown["coverage_ref"] == "transform_shapes:@root"
    assert [r["owner"] for r in rows["transforms"]] == [art], "@package without a package is the file's Artifact"
    assert shard.entities[art]["attributes"]["transforms"] == {"primitives": []}, "no primitives: no purpose"
    [c] = compiler.transform_coverage({"@root": rows})
    assert (c["numerator"], c["denominator"]) == (1, 2)


def test_a_shard_without_shapes_has_no_table():
    assert "transforms" not in compiler._Shard("u").rows()


def test_the_publish_gate_reads_a_transform_owner_as_a_content_id(tmp_path):
    from repo_intel.ingest import secrets
    from repo_intel.ingest.publish_secrets import leaked_secrets

    # A URL password that happens to look like an entity id: the owner field is the id the
    # compiler wrote, as it is for behaviour rows, so it is not a leak.
    owner = "ent_abcdefghijklmnopqrstuvwxyz"
    source = f"DATABASE_URL=postgres://app:{owner}@db/work\n".encode()
    store = ObjectStore(tmp_path / "store", "t")
    manifest = {"entries": [{"path": "config.env", "blob_sha256": store.put_bytes(source).split(":", 1)[1], "secret_findings": secrets.scan(source)}]}
    row = {"owner": owner, "path": "jobs/job.py", "at": [0, 3], "language": "pyspark", "transforms": [{"id": "t1", "op": "read", "at": [0, 3], "inputs": ["a"], "output": "b"}]}
    shard = {"unit": ".", "entities": [], "assertions": [], "evidence": [], "unknowns": [], "transforms": [row]}
    assert leaked_secrets(store, manifest, [shard]) == []
    assert leaked_secrets(store, manifest, [{**shard, "transforms": [{**row, "output": owner}]}]) == ["config.env"], "only the owner field is exempt"


def test_the_architecture_tier_leaves_transforms_out_and_keeps_owner_attributes(compiled):
    store, manifests, _, _ = compiled
    shards = load_shards(store, manifests[0])
    reduced = bundle._architecture_shards(copy.deepcopy(shards), [])
    assert "transforms" in bundle.DETAIL_TABLES and "transforms" in bundle.OPTIONAL_TABLES
    assert not any("transforms" in s for s in reduced.values())
    notebook = _owner("Notebook", {"path": "notebooks/orders_daily.py"})
    kept = {e["id"]: e for s in reduced.values() for e in s["entities"]}
    assert kept[notebook]["attributes"]["transforms"]["purpose"] == "cleaning", "owner attributes are in every tier"


def test_behavior_store_returns_transforms_beside_behaviour(compiled):
    store, manifests, _, _ = compiled
    manifest = Manifest.model_validate(manifests[0].model_dump(mode="json"))
    notebook = _owner("Notebook", {"path": "notebooks/orders_daily.py"})
    found = behavior_store.behavior_of(store, manifest, notebook)
    assert found["units"] == [] and [r["language"] for r in found["transforms"]] == ["pyspark", "sql"], "shapes alone are an answer"
    program = _owner("Program", {"platform": "sas", "name": "sas/programs/monthly_orders.sas"})
    found = behavior_store.behavior_of(store, manifest, program)
    assert found["units"] and len(found["transforms"]) == 6
    index = behavior_store.transform_index(load_shards(store, manifest).values())
    assert list(index) == sorted(index) and notebook in index
    assert behavior_store.behavior_of(store, manifest, _owner("Program", {"platform": "sql", "name": "ghost"})) is None


def test_compiles_are_deterministic(compiled, tmp_path):
    store, manifests, root, shas = compiled
    again = compile_commit(ObjectStore(tmp_path / "cold", "t"), root / "repo", REPO_ID, shas[0]).manifest
    assert again.rkp_hash == manifests[0].rkp_hash
    text = "insert into a select k, sum(v) as s from b group by k"
    assert sql(text) == sql(text)


def test_distinct_on_is_exact_only_with_a_tie_break_order():
    """DISTINCT ON keeps one row per key; which row is fixed only when ORDER BY goes past the keys."""
    def dedupe(text):
        [d] = [t for t in sql(text, dialect="postgres") if t["op"] == "deduplicate"]
        return d.get("primitive"), d.get("candidates")

    assert dedupe("insert into latest select distinct on (id) id, v from orders order by id, updated_at desc") == ("dedupe_by_key", None)
    assert dedupe("insert into latest select distinct on (id) id, v from orders order by id") == (None, ["dedupe_by_key"])
    assert dedupe("insert into latest select distinct on (id) id, v from orders") == (None, ["dedupe_by_key"])
