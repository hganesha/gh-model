"""Data, ML and legacy extractors: the edge cases the F11–F15 fixtures do not pin down."""

import json

from repo_intel.compile.ontology import EntityKind, check_relation
from repo_intel.extractors.data_jobs import DataJobs, _scripts
from repo_intel.extractors.data_python import DataPython
from repo_intel.extractors.legacy import ColdFusion, Cobol, Jcl, Sas, Synapse
from repo_intel.extractors.manifests import CondaEnvironment, Requirements, SetupCfg, SetupPy, effective_declarations
from repo_intel.extractors.sql_lineage import SqlScripts, lineage

BLOB = "cd" * 32


def run(extractor, path, text):
    data = text.encode() if isinstance(text, str) else text
    return extractor.extract(path, data, BLOB)


def names(result, kind):
    return sorted(o.payload["name"] for o in result.observations if o.kind == "entity" and o.payload["kind"] == kind)


def edges(result, predicate=None):
    return {(o.payload["subject"]["name"], o.payload["predicate"], o.payload["object"]["name"]) for o in result.observations if o.kind == "relation" and predicate in (None, o.payload["predicate"])}


def summary(result):
    return next((o.payload for o in result.observations if o.kind == "lineage_summary"), None)


def test_every_relation_fits_the_ontology():
    samples = [
        (DataPython(), "nb.py", "# Databricks notebook source\ndf = spark.read.table('a.b')\ndf.write.saveAsTable('a.c')\n"),
        (DataJobs(), "dvc.yaml", "stages:\n  s:\n    cmd: python -m pkg.run\n    deps: [in.csv]\n    outs: [out.csv]\n"),
        (Cobol(), "X.cbl", "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. X.\n       PROCEDURE DIVISION.\n           CALL 'Y'.\n"),
        (Sas(), "a.sas", "data out.t; set in.s; run;\n"),
        (ColdFusion(), "a.cfm", '<cfquery datasource="d">select * from t</cfquery>'),
    ]
    for extractor, path, text in samples:
        for o in run(extractor, path, text).observations:
            if o.kind == "relation":
                p = o.payload
                assert check_relation(p["predicate"], EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"])) is None, (path, p)


# ---------------------------------------------------------------- manifests


def test_requirements_skip_includes_urls_and_paths_and_scope_dev_files():
    text = "-r base.txt\n-e .\n./vendor/lib\nhttps://x/y.whl\nrequests>=2  # http\nfoo @ git+https://x/foo\n"
    deps = {o.payload["name"]: o.payload for o in run(Requirements(), "svc/requirements.txt", text).observations if o.kind == "dependency_declaration"}
    assert set(deps) == {"requests", "foo"} and deps["foo"]["specifier"] == ""
    dev = [o.payload for o in run(Requirements(), "requirements-test.txt", "pytest\n").observations if o.kind == "dependency_declaration"]
    assert dev[0]["scope"] == "dev"
    assert not Requirements().matches("docs/requirements.md")


def test_setup_py_reads_literal_arguments_only():
    decl = [o.payload for o in run(SetupPy(), "setup.py", "from setuptools import setup\nsetup(name='lib', install_requires=['a>=1', DEP])\n").observations]
    assert any(p.get("name") == "lib" for p in decl) and [p["name"] for p in decl if "specifier" in p] == ["a"]
    assert not run(SetupPy(), "setup.py", "from setuptools import setup\nsetup(name=NAME)\n").observations


def test_setup_cfg_and_conda():
    cfg = run(SetupCfg(), "setup.cfg", "[metadata]\nname = tool\n[options]\ninstall_requires =\n    click>=8\n")
    assert {o.payload.get("name") for o in cfg.observations} >= {"tool", "click"}
    conda = [o.payload for o in run(CondaEnvironment(), "env/environment.yml", "dependencies:\n  - python=3.11\n  - pip\n  - numpy=1.26\n  - pip:\n    - torch==2.3\n").observations if o.kind == "dependency_declaration"]
    assert {(p["ecosystem"], p["name"]) for p in conda} == {("conda", "numpy"), ("pypi", "torch")}


def test_a_named_declaration_replaces_a_weak_one_in_the_same_directory():
    decl = lambda name, d, weak=False: {"payload": {"ecosystem": "pypi", "name": name, "manifest_dir": d, **({"attributes": {"inferred_name": True}} if weak else {})}}
    weak, named, other = ("requirements.txt", decl("root", "", True)), ("pyproject.toml", decl("app", "")), ("tools/requirements.txt", decl("tools", "tools", True))
    kept, redirect = effective_declarations([weak, named, other])
    assert [p for p, _ in kept] == ["pyproject.toml", "tools/requirements.txt"] and redirect == {"requirements.txt": named[1]["payload"]}
    kept, redirect = effective_declarations([weak])
    assert kept == [weak] and redirect == {}


# ---------------------------------------------------------------- SQL lineage


def test_session_tables_are_not_lineage_anywhere_in_the_script():
    sql = "CREATE VOLATILE TABLE vt AS (SELECT * FROM stg.a) WITH DATA ON COMMIT PRESERVE ROWS;\nINSERT INTO edw.b SELECT * FROM vt;\n"
    found = lineage(sql, "teradata")
    assert [n for n, *_ in found.writes] == ["edw.b"] and [n for n, *_ in found.reads] == ["stg.a"] and found.unresolved == 0
    found = lineage("SELECT * INTO #t FROM x; INSERT INTO #t SELECT * FROM y;", "tsql")
    assert not found.writes and found.unresolved == 0, "filling a temporary table is resolved, not unknown"


def test_sql_scripts_name_procedures_and_skip_migrations_and_dbt():
    proc = run(SqlScripts(), "sql/p.sql", "CREATE PROCEDURE [dbo].[p] AS BEGIN INSERT INTO dbo.t SELECT * FROM dbo.s; EXEC dbo.q; END\nGO\n")
    assert names(proc, "Program") == ["dbo.p"] and ("dbo.p", "CALLS", "dbo.q") in edges(proc)
    assert not run(SqlScripts(), "migrations/0001.sql", "INSERT INTO t SELECT * FROM s;").observations
    assert not run(SqlScripts(), "models/m.sql", "select * from {{ ref('x') }}").observations


# ------------------------------------------------------------- Python and jobs


def test_airflow_datasets_bound_to_names():
    src = '''from airflow import DAG, Dataset
from airflow.operators.empty import EmptyOperator
OUT = Dataset("s3://b/out")
with DAG("d", schedule=[Dataset("s3://b/in")]) as dag:
    EmptyOperator(task_id="t", outlets=[OUT])
'''
    result = run(DataPython(), "dags/d.py", src)
    assert ("d", "READS_FROM", "s3://b/in") in edges(result) and ("t", "WRITES_TO", "s3://b/out") in edges(result)


def test_job_commands_resolve_scripts_and_modules():
    assert _scripts("python -u train.py && python3 -m pkg.sub --x") == ["train.py", "pkg/sub.py", "pkg/sub/__main__.py"]
    assert _scripts("bash run.sh") == []


def test_dbt_sources_alias_the_tables_they_name():
    doc = "version: 2\nsources:\n  - name: shop\n    database: prod\n    schema: raw\n    tables:\n      - name: orders\n        identifier: orders_v2\n      - name: customers\n"
    result = run(DataJobs(), "models/sources.yml", doc)
    assert edges(result, "ALIAS_OF") == {("shop.orders", "ALIAS_OF", "prod.raw.orders_v2"), ("shop.customers", "ALIAS_OF", "prod.raw.customers")}
    same = run(DataJobs(), "models/s.yml", "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: t\n")
    assert not edges(same, "ALIAS_OF"), "no alias when the source name is the schema"


def test_not_applicable_files_are_not_reported_as_parsed():
    assert run(DataJobs(), "k8s/deploy.yaml", "kind: Deployment\nmetadata: {name: x}\n").parsed is False
    assert run(DataJobs(), "snapshots/s.sql", "select 1").parsed is False
    assert run(DataPython(), "pkg/util.py", "def f():\n    return 1\n").parsed is False


# -------------------------------------------------------------------- legacy


def test_cobol_reads_only_columns_8_to_72_and_counts_variable_calls():
    line = "           EXEC SQL SELECT A FROM CUSTOMER END-EXEC." + " " * 10 + "IGNORED1"
    src = "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. P1.\n       PROCEDURE DIVISION.\n" + line + "\n           CALL WS-NAME.\n"
    result = run(Cobol(), "P1.cbl", src)
    assert names(result, "Table") == ["customer"] and summary(result)["unresolved"] == 1


def test_jcl_skips_load_libraries_and_temporary_datasets():
    jcl = "//J JOB 1\n//S1 EXEC PGM=MYPGM\n//STEPLIB DD DSN=PROD.LOADLIB,DISP=SHR\n//IN DD DSN=PROD.IN,DISP=SHR\n//TMP DD DSN=&&T,DISP=(NEW,PASS)\n//OUT DD DSN=PROD.HIST(+1),DISP=(NEW,CATLG)\n"
    result = run(Jcl(), "J.jcl", jcl)
    assert names(result, "Dataset") == ["prod.hist", "prod.in"]
    assert ("S1", "RUNS", "MYPGM") in edges(result)


def test_jcl_job_records_its_member_name():
    jcl = "//NIGHTLY  JOB (ACCT),'X'\n//S1 EXEC PGM=MYPGM\n"
    result = run(Jcl(), "jcl/payj030.jcl", jcl)
    jobs = [o.payload for o in result.observations if o.kind == "entity" and o.payload["kind"] == "Job"]
    assert [(j["natural_key"]["name"], j["attributes"]["member"]) for j in jobs] == [("NIGHTLY", "PAYJ030")]


def test_sas_macro_variables_become_placeholders_in_proc_sql_too():
    src = "%let m = 202401;\nproc sql;\n  create table out.t as select * from lib.src_&m. where 1=1;\nquit;\n"
    result = run(Sas(), "p.sas", src)
    assert names(result, "Dataset") == ["lib.src_{m}", "out.t"]
    assert all("x_" not in n for n in names(result, "Dataset"))


def test_coldfusion_datasources_and_component_lookup():
    cfc = '''component {
    remote any function find(id) {
        return queryExecute("SELECT * FROM t WHERE id = :id", {id = id}, {datasource = "appdb"});
    }
    public void function go() { var s = new lib.Helper(); }
}'''
    result = run(ColdFusion(), "app/api/Svc.cfc", cfc)
    assert names(result, "ConfigKey") == ["datasource:appdb"] and names(result, "Endpoint") == ["ANY /app/api/Svc.cfc?method=find"]
    targets = {o for s, p, o in edges(result, "CALLS")}
    assert targets == {"app.api.lib.helper", "app.lib.helper", "lib.helper"}, "every directory the lookup may resolve against"
    assert not names(run(ColdFusion(), "a.cfm", '<cfquery datasource="#ds#">select 1</cfquery>'), "ConfigKey")


def test_synapse_dataset_aliases_one_thing_and_triggers_run_pipelines():
    both = {"name": "ds", "properties": {"type": "X", "typeProperties": {"schema": "s", "table": "t", "location": {"fileSystem": "fs", "folderPath": "p"}}}}
    assert edges(run(Synapse(), "ws/dataset/ds.json", json.dumps(both)), "ALIAS_OF") == {("ds", "ALIAS_OF", "s.t")}
    trig = {"name": "tr", "properties": {"type": "ScheduleTrigger", "pipelines": [{"pipelineReference": {"referenceName": "pl"}}]}}
    assert edges(run(Synapse(), "ws/trigger/tr.json", json.dumps(trig))) == {("tr", "RUNS", "pl")}
    assert run(Synapse(), "ws/pipeline/x.json", "[]").parsed is False
