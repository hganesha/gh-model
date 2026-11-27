"""The shell pack (repo_intel/platforms/shell/): POSIX shell scripts, Windows batch files and
PowerShell scripts, each read from one file's bytes: SQL sent to clients, the tools and
scripts they run, and the files they transfer, with no host, user or credential copied."""

import hashlib
import json
from pathlib import Path

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import validate_unit, walk
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.platforms import CONSTRUCTS, packs, validate_pack
from repo_intel.platforms.shell import PACK
from repo_intel.platforms.shell.source import ShellSource
from repo_intel.platforms.shell.windows import BatchSource, PowerShellSource

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "F29-warehouse-shell"


def run(path: str, data: bytes | str):
    raw = data.encode() if isinstance(data, str) else data
    x = next(e for e in PACK.extractors if e.matches(path))
    return x.extract(path, raw, hashlib.sha256(raw).hexdigest())


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
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])): o.payload["object"] for o in out.observations if o.kind == "relation"}


def constructs(out) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = {}
    for o in out.observations:
        if o.kind == "construct":
            k = (o.payload["construct"], o.payload["modelled"])
            counts[k] = counts.get(k, 0) + 1
    return counts


def units(out) -> list[dict]:
    return [o.payload for o in out.observations if o.kind == "behavior"]


def text_of(out) -> str:
    return json.dumps([o.payload for o in out.observations], sort_keys=True) + json.dumps([d.message for d in out.diagnostics])


def check_all(out) -> None:
    """Every unit validates, every platform node names a registered construct, every relation
    is allowed by the schema."""
    for u in units(out):
        assert validate_unit(u) == [], validate_unit(u)
        for node in walk(u["body"]):
            if node.get("op") == "platform" or node.get("e") == "platform":
                assert node["construct"] in CONSTRUCTS, node["construct"]
    for o in out.observations:
        if o.kind == "relation":
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p


SCRIPT = "etl/nightly.ksh"
KSH = """#!/bin/ksh
set -e
STAGE=stg
DB_PASSWORD="not-a-real-password-for-tests"
CONN="etl/tiger@WHPROD"
export ORACLE_HOME=/opt/oracle/product/19c
export REGION
REGION=emea
typeset -x TNS_ADMIN=/etc/oracle
LANG=C sort -o /tmp/x /tmp/y

sftp -b - etl@files.partner.example <<EOF
cd /outbound
get orders.csv /data/in/orders.csv
put /data/out/ack.txt
bye
EOF

sqlplus -s $CONN <<EOF
WHENEVER SQLERROR EXIT 1
INSERT INTO $STAGE.orders_hist SELECT * FROM stg.orders;
DELETE FROM stg.orders WHERE load_date < SYSDATE - 30;
EXEC order_api.refund_order;
EXEC refresh_stats;
CALL $PROC_NAME;
COMMIT;
EOF

bteq <<EOF
.LOGON tdprod/etl,$DB_PASSWORD;
INSERT INTO mart.orders_daily SELECT order_date, COUNT(*) FROM stg.orders GROUP BY order_date;
.QUIT
EOF

scp /data/out/ack.txt etl@sftp.partner.example:/inbound/ack.txt
pmcmd startworkflow -sv INT_SVC -d DOM_ETL -u etl -p secretpw -f SALES wf_load_sales
pmcmd startworkflow -sv INT_SVC -d DOM_ETL wf_no_folder
pmcmd startworkflow -sv INT_SVC -f "$1" wf_dynamic_folder
dsjob -run -jobstatus DWPROJ LoadCustomers
sas -sysin sas/score.sas
fastload < ../teradata/orders.fastload
hive -f hql/daily.hql
beeline -u jdbc:hive2://hive.example:10000/default -f hql/weekly.hql
impala-shell -f hql/monthly.sql
sh ./archive.sh
"""


def test_pack_is_registered_and_valid():
    assert validate_pack(PACK) == []
    assert packs()["shell"] is PACK
    assert PACK.prefix == "sh" and PACK.version == "1.1.0"
    assert {e.id for e in PACK.extractors} == {"shell.source", "shell.batch", "shell.powershell"}
    assert all(e.version == "1.1.0" for e in PACK.extractors)


def test_pack_inputs_are_left_to_their_pack():
    assert ShellSource().matches("etl/run.ksh") and ShellSource().matches("bin/load.sh")
    assert not ShellSource().matches(".rkp/inputs/abinitio/graphs/load_orders.ksh")
    assert BatchSource().matches("etl/refresh.bat") and not BatchSource().matches(".rkp/inputs/scheduler/run.cmd")
    assert PowerShellSource().matches("etl/a.ps1") and not PowerShellSource().matches(".rkp/inputs/scheduler/a.ps1")


def test_posix_script_program_tasks_and_lineage():
    out = run(SCRIPT, KSH)
    check_all(out)
    ents, rels = entities(out), relations(out)
    me = key("Program", platform="shell", name=SCRIPT)
    assert ents[me]["platform_kind"] == "sh:Script" and ents[me]["shell"] == "ksh"
    # SQL in here-documents, read in the client's dialect; a certain variable is resolved
    assert (me, "WRITES_TO", key("Table", datastore="default", name="stg.orders_hist")) in rels
    assert (me, "READS_FROM", key("Table", datastore="default", name="stg.orders")) in rels
    assert (me, "WRITES_TO", key("Table", datastore="default", name="mart.orders_daily")) in rels
    # transfers: the remote side is <protocol>:<path>
    assert (me, "READS_FROM", key("Dataset", name="sftp:/outbound/orders.csv")) in rels
    assert (me, "WRITES_TO", key("Dataset", name="/data/in/orders.csv")) in rels
    assert (me, "WRITES_TO", key("Dataset", name="sftp:/outbound/ack.txt")) in rels
    assert (me, "WRITES_TO", key("Dataset", name="scp:/inbound/ack.txt")) in rels
    # tasks, keyed {framework: shell, job: <script>, name: <tool>:<target>}
    tasks = {k for k in ents if k[0] == "Task"}
    names = {json.loads(k[1])["name"] for k in tasks}
    assert {"pmcmd:SALES/wf_load_sales", "dsjob:LoadCustomers", "sas:score.sas", "fastload:orders.fastload", "hive:daily.hql", "beeline:weekly.hql",
            "impala-shell:monthly.sql", "sh:archive.sh"} <= names
    for k in tasks:
        nk = json.loads(k[1])
        assert nk["framework"] == "shell" and nk["job"] == SCRIPT
        assert ents[k]["platform_kind"] == "sh:ToolStep"
        assert (me, "CONTAINS", k) in rels


def test_runs_refer_to_what_other_packs_declare_without_declaring_it():
    out = run(SCRIPT, KSH)
    rels = relations(out)
    t = lambda name: key("Task", framework="shell", job=SCRIPT, name=name)  # noqa: E731
    runs = {(s, o) for (s, p, o) in rels if p == "RUNS"}
    assert (t("pmcmd:SALES/wf_load_sales"), key("Job", framework="informatica", name="SALES/wf_load_sales")) in runs
    assert (t("dsjob:LoadCustomers"), key("Job", framework="datastage", name="LoadCustomers")) in runs
    assert (t("sas:score.sas"), key("Program", platform="sas", name="etl/sas/score.sas")) in runs
    assert (t("fastload:orders.fastload"), key("Program", platform="teradata", name="teradata/orders.fastload")) in runs
    # hive -f, beeline -f and impala-shell -f run the Hive Program the hadoop pack declares, beside the file
    for task, path in (("hive:daily.hql", "hql/daily.hql"), ("beeline:weekly.hql", "hql/weekly.hql"), ("impala-shell:monthly.sql", "hql/monthly.sql")):
        assert (t(task), key("Program", platform="hive", name=f"etl/{path}")) in runs
        assert (t(task), key("Artifact", path=f"etl/{path}")) in runs
    assert (t("sh:archive.sh"), key("Program", platform="shell", name="etl/archive.sh")) in runs
    for (s, p, o), obj in rels.items():
        if p == "RUNS":
            assert obj.get("declare") is False, (s, o)
    # nothing the script names but does not define is declared
    declared = {k[0] for k in entities(out)}
    assert "Job" not in declared and "Artifact" not in declared


def test_pmcmd_without_a_literal_folder_stays_dynamic():
    out = run(SCRIPT, KSH)
    ents, rels = entities(out), relations(out)
    dynamic = [k for k in ents if k[0] == "Task" and json.loads(k[1])["name"].startswith("pmcmd") and ents[k].get("dynamic")]
    assert sorted(ents[k]["workflow"] for k in dynamic) == ["wf_dynamic_folder", "wf_no_folder"]
    for k in dynamic:
        assert not any(s == k and p == "RUNS" for (s, p, o) in rels)
    assert not any(o[0] == "Job" and "wf_no_folder" in o[1] for (_, _, o) in rels)
    assert any("folder is not in the file" in d.message for d in out.diagnostics)


def test_sqlplus_exec_of_a_two_part_name_calls_the_package_member_too():
    out = run(SCRIPT, KSH)
    rels = relations(out)
    me = key("Program", platform="shell", name=SCRIPT)
    assert rels[(me, "CALLS", key("Program", platform="oracle", name="order_api.refund_order"))]["declare"] is False
    assert rels[(me, "CALLS", key("Symbol", platform="oracle", program="order_api", name="refund_order"))]["declare"] is False
    assert (me, "CALLS", key("Program", platform="oracle", name="refresh_stats")) in rels
    assert not any(o[0] == "Symbol" and "refresh_stats" in o[1] for (_, _, o) in rels)


def test_a_procedure_named_at_run_time_is_a_dynamic_call():
    out = run(SCRIPT, KSH)
    calls = [n for u in units(out) for n in walk(u["body"]) if n.get("op") == "call" and n["call"]["callee"].get("e") == "platform"]
    assert [c["call"]["callee"]["construct"] for c in calls] == ["sh:sql.dynamic"]
    assert constructs(out)[("sh:sql.dynamic", False)] >= 1
    assert any("named at run time" in d.message for d in out.diagnostics)


def test_environment_values_are_never_copied():
    out = run(SCRIPT, KSH)
    body = units(out)[0]["body"]
    assigned = {n["target"]["name"]: n for n in walk(body) if n.get("op") == "assign" and n.get("target", {}).get("e") == "var"}
    for name in ("ORACLE_HOME", "REGION", "LANG"):
        assert assigned[name]["value"] == {"e": "platform", "construct": "sh:export"}, name
        assert assigned[name]["export"] is True
    declared = {n["name"]: n for n in walk(body) if n.get("op") == "declare"}
    assert declared["TNS_ADMIN"]["value"] == {"e": "platform", "construct": "sh:export"}
    assert assigned["STAGE"]["value"] == {"e": "lit", "value": "stg", "type": "string"}  # not exported: a value
    assert assigned["DB_PASSWORD"]["value"] == {"e": "platform", "construct": "sh:sensitive-value"}
    text = text_of(out)
    for value in ("/opt/oracle/product/19c", "/etc/oracle", '"emea"'):
        assert value not in text


def test_no_host_user_password_or_connection_string_is_copied():
    out = run(SCRIPT, KSH)
    text = text_of(out)
    for leak in ("not-a-real-password-for-tests", "tiger", "WHPROD", "etl@", "files.partner.example", "sftp.partner.example", "tdprod",
                 "hive.example", "jdbc:hive2", "secretpw", "INT_SVC", "DOM_ETL"):
        assert leak not in text, leak


def test_construct_counts():
    out = run(SCRIPT, KSH)
    c = constructs(out)
    assert c[("sh:sql.sqlplus", True)] == 1 and c[("sh:sql.bteq", True)] == 1
    assert c[("sh:transfer.sftp", True)] == 1 and c[("sh:transfer.scp", True)] == 1
    assert c[("sh:run.pmcmd", True)] == 1 and c[("sh:run.dynamic", False)] == 2
    assert c[("sh:run.dsjob", True)] == 1 and c[("sh:run.sas", True)] == 1 and c[("sh:run.fastload", True)] == 1
    assert c[("sh:sql.client-file", True)] == 3
    assert c[("sh:heredoc", True)] == 3
    assert c[("sh:sensitive-value", True)] == 2
    assert c[("sh:export", True)] >= 4
    assert c[("sh:sql.client-command", False)] >= 3
    assert all(cid in CONSTRUCTS for cid, _ in c)


def test_dtexec_runs_the_package_job_by_its_path_or_keeps_the_catalog_path():
    out = run("jobs/run_packages.sh", "#!/bin/sh\ndtexec /F packages/Load.dtsx\ndtexec /SQL \"\\\\Maint\\\\Nightly\"\ndtexec /ISServer \"\\\\SSISDB\\\\Sales\\\\Daily.dtsx\" /Server sql01.corp.example\n")
    check_all(out)
    ents, rels = entities(out), relations(out)
    t = lambda name: key("Task", framework="shell", job="jobs/run_packages.sh", name=name)  # noqa: E731
    assert (t("dtexec:Load.dtsx"), "RUNS", key("Job", framework="ssis", name="jobs/packages/Load.dtsx")) in rels
    assert (t("dtexec:Load.dtsx"), "RUNS", key("Job", framework="ssis", name="packages/Load.dtsx")) in rels
    assert (t("dtexec:Load.dtsx"), "RUNS", key("Artifact", path="jobs/packages/Load.dtsx")) in rels
    assert rels[(t("dtexec:Load.dtsx"), "RUNS", key("Job", framework="ssis", name="packages/Load.dtsx"))]["declare"] is False
    for name, path in (("dtexec:Nightly", "/Maint/Nightly"), ("dtexec:Daily", "/SSISDB/Sales/Daily.dtsx")):
        assert ents[t(name)]["catalog_path"] == path
        assert not any(s == t(name) and p == "RUNS" for (s, p, o) in rels)
    assert "sql01.corp.example" not in text_of(out)


def test_batch_file():
    path = "etl/refresh_marts.bat"
    out = run(path, (FIXTURE / "base" / path).read_bytes())
    check_all(out)
    ents, rels = entities(out), relations(out)
    me = key("Program", platform="shell", name=path)
    assert ents[me]["platform_kind"] == "sh:BatchFile"
    assert rels[(me, "CALLS", key("Program", platform="tsql", name="dbo.refresh_marts"))]["declare"] is False
    t = key("Task", framework="shell", job=path, name="dtexec:ExportMarts.dtsx")
    assert (t, "RUNS", key("Job", framework="ssis", name="etl/packages/ExportMarts.dtsx")) in rels
    assert (t, "RUNS", key("Artifact", path="etl/packages/ExportMarts.dtsx")) in rels
    assert (me, "CONTAINS", key("Task", framework="shell", job=path, name="sqlcmd:rebuild_indexes.sql")) in rels
    assert len(units(out)) == 1 and units(out)[0]["kind"] == "open_code"
    c = constructs(out)
    assert c[("sh:bat.set", True)] == 1 and c[("sh:sql.sqlcmd", True)] == 2 and c[("sh:run.dtexec", True)] == 1
    assert "MART_SERVER" not in json.dumps([o.payload for o in out.observations if o.kind == "entity"])


def test_powershell_script():
    path = "etl/archive.ps1"
    out = run(path, (FIXTURE / "base" / path).read_bytes())
    check_all(out)
    ents, rels = entities(out), relations(out)
    me = key("Program", platform="shell", name=path)
    assert ents[me]["platform_kind"] == "sh:PowerShellScript"
    assert (me, "WRITES_TO", key("Table", datastore="default", name="mart.orders_archive")) in rels
    assert (me, "READS_FROM", key("Table", datastore="default", name="stg.orders")) in rels
    assert units(out) == []  # PowerShell is read for commands only
    assert constructs(out)[("sh:ps.invoke-sqlcmd", True)] == 1
    assert "sql01.corp.example" not in text_of(out)


def test_fixture_scripts_keep_the_planted_password_out():
    for path in ("etl/nightly_orders.ksh", "etl/send_extract.sh", "etl/refresh_marts.bat", "etl/archive.ps1"):
        out = run(path, (FIXTURE / "base" / path).read_bytes())
        check_all(out)
        text = text_of(out)
        for leak in ("fixture-not-a-real-password-0029", "partner.example", "etl@", "WHPROD", "tdprod", "etl-team@example.com", "sql01.corp.example"):
            assert leak not in text, (path, leak)


def test_fixture_nightly_job():
    path = "etl/nightly_orders.ksh"
    out = run(path, (FIXTURE / "base" / path).read_bytes())
    rels = relations(out)
    t = lambda name: key("Task", framework="shell", job=path, name=name)  # noqa: E731
    assert (t("pmcmd:SALES/wf_load_sales"), "RUNS", key("Job", framework="informatica", name="SALES/wf_load_sales")) in rels
    assert (t("fastload:orders.fastload"), "RUNS", key("Program", platform="teradata", name="teradata/orders.fastload")) in rels
    me = key("Program", platform="shell", name=path)
    assert (me, "CALLS", key("Symbol", platform="oracle", program="stg", name="refresh_order_stats")) in rels
