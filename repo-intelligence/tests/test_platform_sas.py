"""The SAS platform pack (repo_intel/platforms/sas): lexing, macro expansion, lowering to the
behavioural IR, entities and relations, native inputs, and the superset of legacy.sas."""

import hashlib
import json
import time
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile import behavior
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.extractors import extract_file  # noqa: F401  (the package is fully loaded)
from repo_intel.extractors.legacy import Sas
from repo_intel.platforms import packs, superseded, validate_pack
from repo_intel.platforms.sas import PACK, SasInputs, SasSource
from repo_intel.platforms.sas.lexer import lex
from repo_intel.platforms.sas.macro import Macros, placeholders
from repo_intel.platforms.sas.parse import parse

FIXTURES = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures"


def run(text: str, path: str = "sas/prog.sas", extractor=None):
    data = text.encode()
    return (extractor or SasSource()).extract(path, data, hashlib.sha256(data).hexdigest())


def ref(x) -> tuple:
    return x if isinstance(x, str) else (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


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
            key = (o.payload["construct"], o.payload["modelled"])
            counts[key] = counts.get(key, 0) + 1
    return counts


def units(out) -> list[dict]:
    return [o.payload for o in out.observations if o.kind == "behavior"]


def prog(name):
    return ("Program", json.dumps({"name": name, "platform": "sas"}, sort_keys=True))


def ds(name):
    return ("Dataset", json.dumps({"name": name}))


def ck(name):
    return ("ConfigKey", json.dumps({"name": name}))


def cl(name):
    return ("CodeList", json.dumps({"name": name, "platform": "sas"}, sort_keys=True))


def rep(name):
    return ("Report", json.dumps({"name": name, "platform": "sas"}, sort_keys=True))


def walk_nodes(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk_nodes(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_nodes(v)


# ---------------------------------------------------------------------- the pack


def test_pack_is_registered_valid_and_supersedes_legacy():
    assert packs()["sas"] is PACK
    assert validate_pack(PACK) == []
    assert "legacy.sas" in superseded()
    assert {e.id for e in PACK.extractors} == {"sas.source", "sas.inputs"}
    assert PACK.inputs == (".rkp/inputs/sas/**",)
    assert len(PACK.constructs) >= 60


def test_every_construct_and_term_the_code_uses_is_listed():
    ids = {c.id for c in PACK.constructs}
    terms = {t.name for t in PACK.terms}
    source = "".join(p.read_text() for p in (Path(__file__).resolve().parents[1] / "repo_intel" / "platforms" / "sas").glob("*.py"))
    import re

    used = set(re.findall(r'"(sas:[a-z][a-z0-9_]*)"', source))
    assert used <= ids, sorted(used - ids)
    used_terms = set(re.findall(r'"(sas:[A-Z][A-Za-z]*)"', source))
    assert used_terms <= terms, sorted(used_terms - terms)


# ---------------------------------------------------------------------- superset of legacy.sas


def _legacy_keys(out) -> set[tuple]:
    keys = set()
    for o in out.observations:
        if o.kind == "entity":
            keys.add(("E", *ref(o.payload)))
        elif o.kind == "relation":
            keys.add(("R", ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])))
    return keys


@pytest.mark.parametrize("rel", ["sas/monthly_load.sas", "sas/macros/audit.sas"])
def test_superset_of_legacy_sas_on_f14(rel):
    """Every entity and relation legacy.sas emits on F14, with the same kind and natural key."""
    data = (FIXTURES / "F14-mainframe-sas" / "base" / rel).read_bytes()
    blob = hashlib.sha256(data).hexdigest()
    old = _legacy_keys(Sas().extract(rel, data, blob))
    new = _legacy_keys(SasSource().extract(rel, data, blob))
    assert old, "legacy.sas found nothing: the comparison would be empty"
    assert old <= new, sorted(old - new, key=str)


def test_legacy_attributes_kept_on_f14():
    data = (FIXTURES / "F14-mainframe-sas" / "base" / "sas" / "monthly_load.sas").read_bytes()
    found = entities(SasSource().extract("sas/monthly_load.sas", data, "0" * 64))
    assert found[prog("sas/monthly_load.sas")]["kind"] == "program"
    assert found[ds("stage.customers_{month}")]["parameterized"] is True
    assert found[ds("rpt.customer_extract")]["platform"] == "sas"
    assert found[ck("libname:rpt")]["engine"] == "oracle"
    assert found[ck("libname:rpt")]["source"] == "sas_libname"
    assert ds("work.sorted") not in found and ds("stage.customers_x_____") not in found


# ---------------------------------------------------------------------- lexing and parsing


def test_comments_quoting_and_datalines_do_not_split_or_leak_statements():
    text = """* a comment with don't ;
%let x = %str(a;b);
/* data hidden.ds; */
data rpt.out; input a; datalines;
1;
run;
%put %nrstr(semi;colon);
data rpt.two; set rpt.out; run;
"""
    p = parse(lex(text))
    kinds = [s.kind for s in p.stmts]
    assert kinds.count("data") == 2
    assert "hidden" not in lex(text).clean()
    found = entities(run(text))
    assert ds("hidden.ds") not in found
    assert {ds("rpt.out"), ds("rpt.two")} <= set(found)


def test_macro_definitions_nest_and_calls_without_semicolons_end_statements():
    text = """%macro outer(n=);
  %macro inner; data rpt.inner; set rpt.a; run; %mend inner;
  %inner
  %dyn_&n
%mend outer;
%outer(n=1)
data rpt.after; set rpt.b; run;
"""
    p = parse(lex(text))
    assert [m.name for m in p.macros] == ["outer", "inner"]
    assert p.macros[1].parent == 0
    out = run(text)
    rels = relations(out)
    assert (prog("outer"), "CALLS", prog("inner")) in rels
    assert (prog("sas/prog.sas"), "CALLS", prog("outer")) in rels
    assert (prog("sas/prog.sas"), "WRITES_TO", ds("rpt.after")) in rels  # %mend closed the macro
    assert any("names its macro at run time" in d.message for d in out.diagnostics)


# ---------------------------------------------------------------------- macro expansion


def _expand(text: str, ref_text: str, at: str, scope=None):
    p = parse(lex(text))
    m = Macros(p)
    out = m.expand(ref_text, scope, text.index(at))
    return out, m


@pytest.mark.parametrize("ref_text, want", [
    ("&lib..x", "rpt.x"),
    ("&lib.x", "rptx"),
    ("&&lib", "rpt"),
    ("&&&ptr", "rpt"),  # &&&ptr -> &lib -> rpt
    ("&lib..sales_&yr", "rpt.sales_2024"),
    ("stage.customers_&month", "stage.customers_{month}"),
    ("&quoted", "a;b"),
])
def test_symbolic_macro_expansion(ref_text, want):
    text = "%let lib = rpt;\n%let yr = 2024;\n%let ptr = lib;\n%let quoted = %str(a;b);\n%let month = %sysfunc(today());\ndata x; run;\n"
    out, _ = _expand(text, ref_text, "data x")
    assert placeholders(out) == want


def test_expansion_reports_why_a_reference_stays_dynamic():
    text = """%let once = a;
%let twice = a;
%let twice = b;
%if &cond %then %let inside = c;
data _null_; call symputx('runtime', 1); run;
%macro m(p=); data x; set &p; run; %mend;
data x; run;
"""
    _, m = _expand(text, "&twice &inside &runtime &undefined &sysdate", "data x; run;\n")
    assert "more than once" in m.dynamic["twice"]
    assert "%IF" in m.dynamic["inside"]
    assert "run time" in m.dynamic["runtime"]
    assert "not assigned" in m.dynamic["undefined"]
    assert "automatic" in m.dynamic["sysdate"]
    out, m = _expand(text, "&p", "set &p", scope=0)
    assert placeholders(out) == "{p}" and "parameter" in m.dynamic["p"]


def test_reference_before_its_let_and_computed_names_stay_dynamic():
    out, m = _expand("data x; run;\n%let late = 1;\n", "&late", "data x")
    assert placeholders(out) == "{late}" and "before" in m.dynamic["late"]
    out, m = _expand("%let a = 1;\n%let &a = 2;\ndata x; run;\n", "&a", "data x")
    assert placeholders(out) == "{a}" and "computed names" in m.dynamic["a"]


def test_macro_local_let_resolves_only_inside_its_macro():
    text = "%let g = open;\n%macro m; %let q = inner; data out.&q &g; run; %mend;\n"
    found = entities(run(text))
    assert ds("out.inner") in found
    out = run("%let lib = rpt;\n%macro m; data &lib..x; run; %mend;\n")
    assert ds("rpt.x") not in entities(out) and ds("{lib}.x") in entities(out)


def test_expansion_is_bounded():
    text = "%let e = " + "x" * 48 + ";\n%let d = &e&e&e&e;\n%let c = &d&d&d&d;\n%let b = &c&c&c&c;\n%let a = &b&b&b&b;\ndata x; run;\n"
    out, _ = _expand(text, "&d", "data x")
    assert out == "x" * 192
    out, m = _expand(text, "&a", "data x")
    assert placeholders(out) == "{a}" and "bounds" in m.dynamic["a"]  # 12288 characters: dynamic, never a huge name
    out, _ = _expand(text, "&d&d&d&d&d&d&d&d&d&d&d&d&d&d&d&d&d&d&d&d&d&d", "data x")
    assert out is None  # the reference itself expands beyond MAX_LENGTH
    out, _ = _expand("%let a = &a.x;\ndata x; run;\n", "&a", "data x")  # refers to itself
    assert placeholders(out) == "{a}x"
    out, _ = _expand("%let a = 1;\n%let b = &a;\ndata x; run;\n%let a = 2;\n", "&b", "data x")
    assert placeholders(out) == "{a}"  # b is pinned, a is not: partly resolved


def test_resolved_and_placeholder_dataset_names():
    text = "%let lib = rpt;\ndata &lib..clean; set raw.in_&month; run;\n"
    out = run(text)
    found = entities(out)
    assert found[ds("rpt.clean")]["macro_resolved"] is True
    assert found[ds("raw.in_{month}")]["parameterized"] is True
    assert any("&month stays dynamic" in d.message for d in out.diagnostics)


# ---------------------------------------------------------------------- behaviour units


KITCHEN = """
%let env = prod;
libname rpt oracle user=scott password=tiger path="db";
data rpt.final (keep=id total);
  set rpt.a rpt.b(in=inb);
  by id;
  retain seen 0;
  array vals{3} v1-v3;
  if _n_ = 1 then do;
    declare hash h(dataset: "rpt.lookup");
    h.definekey('id');
    h.definedone();
  end;
  select (status);
    when ('A', 'B') total = 1;
    otherwise total = 0;
  end;
  do i = 1 to dim(vals) by 1;
    total + vals{i};
    if total > 5 then leave;
  end;
  do while (total < 10); total = total + 1; end;
  call symputx('last_id', id);
  label = put(code, $region.);
  if first.id and not last.id then output;
  else delete;
  if total;
run;
proc sql;
  create table rpt.summary as select id, sum(total) as t format=comma12. from rpt.final group by id;
  select count(*) into :n trimmed from rpt.summary;
quit;
%macro loop(n=3);
  %local i;
  %do i = 1 %to &n;
    %if &i = 2 %then %put two; %else %put other;
  %end;
%mend loop;
%loop(n=2)
"""


def test_units_are_valid_one_per_data_step_sql_step_macro_and_open_code():
    out = run(KITCHEN)
    us = units(out)
    for u in us:
        assert behavior.validate_unit(u) == [], behavior.validate_unit(u)
    owners = [u["owner"]["natural_key"]["name"] for u in us]
    assert owners.count("sas/prog.sas") == 3  # the DATA step, the PROC SQL step and open code
    assert owners.count("loop") == 1
    data_step = next(u for u in us if u["body"] and u["body"][0].get("kind") == "implicit")
    loop = data_step["body"][0]
    assert loop["op"] == "loop" and loop["detail"]["data"] == ["rpt.final"]
    ops = {n.get("op") for n in walk_nodes(loop["body"])}
    assert {"if", "switch", "loop", "assign", "declare", "output", "break", "continue"} <= ops
    sql = next(u for u in us if u["body"] and u["body"][0].get("op") == "query")
    assert [q["op"] for q in sql["body"]] == ["query", "query"]
    pack_constructs = {c.id for c in PACK.constructs}
    for u in us:
        for n in walk_nodes(u["body"]):
            if n.get("op") == "platform" or n.get("e") == "platform":
                assert n["construct"] in pack_constructs, n["construct"]


def test_constructs_are_counted_with_whether_they_were_modelled():
    counts = constructs(run(KITCHEN))
    for cid in ("sas:data_step", "sas:set", "sas:by_group", "sas:retain", "sas:array", "sas:hash_object", "sas:select",
                "sas:do_loop", "sas:sum_statement", "sas:call_symput", "sas:format_use", "sas:subsetting_if", "sas:proc_sql",
                "sas:sql_into_macro", "sas:macro_definition", "sas:macro_do", "sas:macro_if", "sas:libname", "sas:let"):
        assert counts.get((cid, True)), cid
    known = {c.id for c in PACK.constructs}
    assert {cid for cid, _ in counts} <= known


def test_data_step_lowering_of_expressions():
    out = run("data rpt.x; set rpt.y; z = a || b; w = c ** 2; if d =: 'AB' and e in (1, 2) and f ne . then g = max(h, i); run;\n")
    (loop,) = units(out)[0]["body"]
    ops = {n.get("op") for n in walk_nodes(loop) if n.get("e") == "binop"}
    assert {"&", "^", "like", "in", "!=", "and"} <= ops
    assert any(n.get("e") == "lit" and n.get("type") == "null" for n in walk_nodes(loop))


# ---------------------------------------------------------------------- entities and relations


def test_formats_are_code_lists_written_by_proc_format_and_read_where_applied():
    text = """proc format library=fmtlib;
  value $region 'N','NE' = 'North' other = 'Other';
  invalue grade 'A' = 1 'B' = 2;
  picture pct low-high = '009.9%';
run;
data rpt.out; set rpt.in; r = put(code, $region.); format amt comma12.2 grade grade.; run;
"""
    out = run(text)
    found = entities(out)
    assert found[cl("$region")]["platform_kind"] == "sas:Format"
    assert found[cl("$region")]["entries"][0] == {"code": "N, NE", "label": "North"}
    assert found[cl("grade")]["platform_kind"] == "sas:Informat"
    assert found[cl("pct")]["fidelity"] == "unmapped"
    rels = relations(out)
    assert (prog("sas/prog.sas"), "WRITES_TO", cl("$region")) in rels
    assert (prog("sas/prog.sas"), "READS_FROM", cl("$region")) in rels
    assert (prog("sas/prog.sas"), "READS_FROM", cl("grade")) in rels
    assert cl("comma") not in found and not any(o == cl("comma") for _, _, o in rels)


def test_ods_documents_standalone_reports_and_exports():
    text = """title "Sales";
ods pdf file="/out/&env/sales.pdf";
proc report data=rpt.s; run;
proc print data=rpt.s; run;
ods pdf close;
proc print data=rpt.t; run;
proc tabulate data=rpt.t; class a; table a; run;
proc export data=rpt.t outfile="/exp/t.xlsx" dbms=xlsx replace; run;
proc means data=rpt.t noprint; output out=rpt.m mean=; run;
"""
    out = run(text)
    found = entities(out)
    doc = found[rep("sales.pdf")]
    assert doc["platform_kind"] == "sas:OdsDocument" and doc["procedures"] == ["print", "report"] and doc["title"] == "Sales"
    assert found[rep("sas/prog.sas#print1")]["platform_kind"] == "sas:Report"
    assert found[rep("sas/prog.sas#tabulate1")]["sources"] == ["rpt.t"]
    assert found[rep("t.xlsx")]["format"] == "xlsx"
    rels = relations(out)
    for name in ("sales.pdf", "sas/prog.sas#print1", "t.xlsx"):
        assert (prog("sas/prog.sas"), "PRODUCES", rep(name)) in rels
    assert not any("/out" in k[1] or "/exp" in k[1] for k in found)  # base names, never paths
    assert (prog("sas/prog.sas"), "WRITES_TO", ds("rpt.m")) in rels


def test_stored_process_prompts_become_a_form():
    text = "*ProcessBody;\n%global region year;\n%stpbegin;\nproc print data=rpt.s; where region = \"&region\"; run;\n%stpend;\n"
    out = run(text, path="stp/sales.sas")
    found = entities(out)
    assert found[prog("stp/sales.sas")]["platform_kind"] == "sas:StoredProcess"
    assert found[rep("stp/sales.sas#output")]["procedures"] == ["print"]
    form = ("Form", json.dumps({"name": "prompts", "screen": "stp/sales.sas"}, sort_keys=True))
    control = ("UIControl", json.dumps({"container": "stp/sales.sas#prompts", "name": "region"}, sort_keys=True))
    assert form in found and control in found
    rels = relations(out)
    assert (form, "SUBMITS_TO", prog("stp/sales.sas")) in rels
    assert (form, "CONTAINS", control) in rels
    (open_code,) = [u for u in units(out) if u["owner"]["natural_key"]["name"] == "stp/sales.sas" and not u["body"][0].get("kind") == "implicit"]
    assert [p["name"] for p in open_code["params"]] == ["region", "year"]


def test_derived_from_follows_work_datasets():
    text = """data tmp; set raw.a; run;
proc sort data=tmp out=tmp2; by k; run;
proc sql; create table mart.out as select * from tmp2 join raw.b on 1=1; quit;
"""
    rels = relations(run(text))
    assert (ds("mart.out"), "DERIVED_FROM", ds("raw.a")) in rels
    assert (ds("mart.out"), "DERIVED_FROM", ds("raw.b")) in rels
    assert not any(s == ds("work.tmp") or o == ds("work.tmp") for s, _, o in rels)


def test_names_never_values():
    text = """libname db oracle user=scott password="{SAS002}ABCDEF" path="prod_tns" schema=sales;
filename pw pipe "cat /etc/secret";
proc sql;
  connect to oracle (user=scott password=tiger path=prod);
  create table rpt.r as select * from connection to oracle (select * from hidden.t);
  disconnect from oracle;
quit;
proc pwencode in='tiger'; run;
proc http url="https://user:pass@example.com/api" out=resp; run;
"""
    out = run(text)
    blob = json.dumps([o.payload for o in out.observations]) + json.dumps([d.message for d in out.diagnostics])
    for secret in ("tiger", "SAS002", "prod_tns", "/etc/secret", "example.com", "user:pass", "scott"):
        assert secret not in blob, secret
    found = entities(out)
    assert found[ck("libname:db")]["engine"] == "oracle"
    assert found[ck("filename:pw")]["device"] == "pipe"
    assert ds("hidden.t") not in found and ds("rpt.r") in found


def test_includes_and_environment_variables():
    text = """%include "../common/setup.sas";
%include "/abs/path.sas";
%include "&root/x.sas";
%let home = %sysget(SALES_HOME);
data _null_; x = sysget('OTHER'); run;
"""
    out = run(text, path="sas/jobs/run.sas")
    rels = relations(out)
    assert (prog("sas/jobs/run.sas"), "CALLS", prog("sas/common/setup.sas")) in rels
    assert (prog("sas/jobs/run.sas"), "REFERENCES_CONFIG", ck("sas_env:SALES_HOME")) in rels
    assert (prog("sas/jobs/run.sas"), "REFERENCES_CONFIG", ck("sas_env:OTHER")) in rels
    assert sum("%INCLUDE" in d.message for d in out.diagnostics) == 2


def test_every_relation_satisfies_the_ontology():
    for text in (KITCHEN, (FIXTURES / "F21-sas-analytics" / "base" / "sas" / "programs" / "sales_summary.sas").read_text()):
        for o in run(text).observations:
            if o.kind == "relation":
                p = o.payload
                assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"])) is None, p


# ---------------------------------------------------------------------- native inputs


def test_autoexec_under_inputs_declares_libraries_without_paths():
    path = ".rkp/inputs/sas/autoexec.sas"
    text = (FIXTURES / "F21-sas-analytics" / "base" / path).read_text()
    out = run(text, path=path)
    found = entities(out)
    assert found[prog(path)]["role"] == "autoexec"
    assert found[ck("libname:mart")]["declared_in"] == "autoexec.sas"
    assert "/sasdata" not in json.dumps([o.payload for o in out.observations])


def test_metadata_export_jobs_tasks_and_prompts():
    path = ".rkp/inputs/sas/metadata/sales_jobs.xml"
    data = (FIXTURES / "F21-sas-analytics" / "base" / path).read_bytes()
    assert SasInputs().matches(path) and not SasInputs().matches("sas/x.xml")
    out = SasInputs().extract(path, data, "0" * 64)
    found = entities(out)
    job = ("Job", json.dumps({"framework": "sas", "name": "monthly_sales"}, sort_keys=True))
    task = ("Task", json.dumps({"framework": "sas", "job": "monthly_sales", "name": "summarise"}, sort_keys=True))
    first = ("Task", json.dumps({"framework": "sas", "job": "monthly_sales", "name": "load_formats"}, sort_keys=True))
    assert job in found and task in found
    rels = relations(out)
    assert (job, "CONTAINS", task) in rels
    assert (task, "DEPENDS_ON", first) in rels
    assert (task, "RUNS", prog("sas/programs/sales_summary.sas")) in rels
    assert (task, "WRITES_TO", ds("mart.region_summary")) in rels
    assert "SASApp" not in json.dumps([o.payload for o in out.observations])
    for o in out.observations:
        if o.kind == "relation":
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"])) is None, p


def test_metadata_export_refuses_entities_and_bad_xml():
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><Job Name="&a;"/>'
    out = SasInputs().extract(".rkp/inputs/sas/x.xml", bomb, "0" * 64)
    assert not out.parsed and not out.observations
    out = SasInputs().extract(".rkp/inputs/sas/x.xml", b"<Job Name='a'>", "0" * 64)
    assert not out.parsed


def test_config_file_declares_environment_variables_by_name_only():
    data = b"-MEMSIZE 4G\n-SET RPT_ENV prod\n-set db_password hunter2\n"
    out = SasInputs().extract(".rkp/inputs/sas/sasv9.cfg", data, "0" * 64)
    found = entities(out)
    assert ck("sas_env:RPT_ENV") in found and ck("sas_env:DB_PASSWORD") in found
    blob = json.dumps([o.payload for o in out.observations])
    assert "hunter2" not in blob and "prod" not in blob.replace("platform", "")


# ---------------------------------------------------------------------- robustness


@pytest.mark.parametrize("text", [
    "%macro a; data x; set y; /* never closed",
    "data x; y = '" + "a" * 50000,
    "data x; y = " + "(" * 5000 + "1" + ")" * 5000 + "; run;",
    "%let a = 1;\ndata x.&" + "&" * 5000 + "a; run;",
    "%let a = &b;\n%let b = &a;\ndata rpt.&a; run;\n",
    "proc sql; create table a.b as select * from (((( ; quit;",
    "\x00\x01 data; ; ; run; %mend; %end; end;",
    "",
])
def test_hostile_input_finishes_quickly_and_validly(text):
    started = time.perf_counter()
    out = run(text)
    assert time.perf_counter() - started < 5
    for u in units(out):
        assert behavior.validate_unit(u) == []


def test_lexing_parsing_and_expansion_are_linear():
    unit = "%let lib{i} = rpt;\ndata &lib{i}..out{i}; set rpt.in{i}; if a > 1 then b = put(c, $f{i}.); run;\n%macro m{i}(p=); %if &p = 1 %then %put y; %mend;\n"

    def cost(n: int) -> float:
        text = "".join(unit.replace("{i}", str(i)) for i in range(n))
        started = time.perf_counter()
        p = parse(lex(text))
        m = Macros(p)
        for i in range(n):
            m.expand(f"&lib{i}..x", None, len(text))
        return time.perf_counter() - started

    cost(200)  # warm up
    small, large = min(cost(1000) for _ in range(2)), min(cost(4000) for _ in range(2))
    assert large < small * 8, (small, large)  # 4x the input, well under 16x


def test_undecodable_bytes_are_reported_not_raised():
    out = SasSource().extract("x.sas", b"\x81\x8d\x8f\x90\x9d", "0" * 64)
    assert out.parsed or out.diagnostics
