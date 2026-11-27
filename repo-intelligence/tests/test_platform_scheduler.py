"""The batch scheduler pack (repo_intel/platforms/scheduler): Control-M, AutoSys, IBM Workload
Scheduler, CA-7, cron and Windows Task Scheduler exports read by `scheduler.exports`."""

import hashlib
import json
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.platforms import check_platform_kind, packs, validate_pack
from repo_intel.platforms.scheduler import PACK, SchedulerExports
from repo_intel.platforms.scheduler import autosys
from repo_intel.platforms.scheduler.common import path_suffixes, starts, words
from repo_intel.platforms.scheduler.source import detect

FIXTURES = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures"
F35 = FIXTURES / "F35-scheduler-exports" / "base"
INPUTS = ".rkp/inputs/scheduler/"
EXPORTS = sorted(str(p.relative_to(F35)) for p in (F35 / INPUTS).rglob("*") if p.is_file())
# Names of machines, users and values in the fixture exports that must never be copied.
NEVER_COPIED = ("batchhost01", "batchsvc", "orders_stage", "ops-team", "Order extract failed", "rpthost02", "rptsvc", "cicsagent", "zosagent", "paysvc",
                "gx,ge", "/var/log/rpt", "finbatch", "-period current", "batch-ops", "/bin/bash", "--days 14", "--incremental", "CONFIRM THE BANK", "svc_tasks",
                "LeastPrivilege", "EMEA", "ExecutionPolicy", "ctm-prod", "ctm-zos", '"full"', "report.out", "-log")


def run(text: str | bytes, path: str = INPUTS + "export.txt"):
    data = text if isinstance(text, bytes) else text.encode()
    return SchedulerExports().extract(path, data, hashlib.sha256(data).hexdigest())


def fixture(path: str):
    return run((F35 / path).read_bytes(), path)


def ref(x) -> tuple:
    return x if isinstance(x, str) else (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def key(kind: str, **nk) -> tuple:
    return (kind, json.dumps(nk, sort_keys=True))


def job(framework: str, name: str) -> tuple:
    return key("Job", framework=framework, name=name)


def art(path: str) -> tuple:
    return key("Artifact", path=path)


def prog(platform: str, name: str) -> tuple:
    return key("Program", platform=platform, name=name)


def entities(out) -> dict[tuple, dict]:
    found: dict[tuple, dict] = {}
    for o in out.observations:
        if o.kind == "entity":
            assert ref(o.payload) not in found, f"declared twice: {o.payload}"
            found[ref(o.payload)] = o.payload["attributes"]
    return found


def relations(out) -> dict[tuple, dict]:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])): {**o.payload["qualifiers"], "_ev": o.evidence_class.value if hasattr(o.evidence_class, "value") else o.evidence_class}
            for o in out.observations if o.kind == "relation"}


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
    return json.dumps([o.payload for o in out.observations], sort_keys=True) + json.dumps(messages(out))


# ---------------------------------------------------------------------- the pack


def test_pack_is_registered_and_valid():
    assert packs()["scheduler"] is PACK
    assert validate_pack(PACK) == []
    assert PACK.prefix == "sched" and PACK.version == "1.1.0"
    assert {e.id for e in PACK.extractors} == {"scheduler.exports"}
    assert PACK.inputs == (".rkp/inputs/scheduler/**",)
    assert {t.core for t in PACK.terms} == {EntityKind.JOB, EntityKind.ENDPOINT}
    assert all(t.fidelity == "lossy" for t in PACK.terms)
    assert len(PACK.constructs) >= 70


def test_the_extractor_claims_only_scheduler_inputs():
    x = SchedulerExports()
    for path in (INPUTS + "controlm/orders.xml", INPUTS + "a.jil", INPUTS + "cron/crontab", ".RKP/INPUTS/SCHEDULER/x.txt"):
        assert x.matches(path), path
    for path in ("jcl/PAYJ010.jcl", "crontab", ".rkp/inputs/cfml/neo-cron.xml", "scheduler/orders.jil", INPUTS + ".hidden"):
        assert not x.matches(path), path


def test_the_pack_imports_extractors_only_inside_functions():
    import ast

    for p in (Path(__file__).resolve().parents[1] / "repo_intel" / "platforms" / "scheduler").glob("*.py"):
        tree = ast.parse(p.read_text())
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("repo_intel.extractors"), p.name
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("repo_intel.extractors") for a in node.names), p.name


def test_every_term_and_construct_the_code_names_is_listed():
    import re

    source = "".join(p.read_text() for p in (Path(__file__).resolve().parents[1] / "repo_intel" / "platforms" / "scheduler").glob("*.py") if p.name != "vocabulary.py")
    terms = {t.name for t in PACK.terms}
    used = set(re.findall(r'"(sched:[A-Z][A-Za-z0-9]*)"', source))
    assert used and used <= terms, sorted(used - terms)
    ids = {c.id for c in PACK.constructs}
    named = set(re.findall(r'"(sched:[a-z0-9]+\.[a-z0-9.-]+)"', source)) | {f"sched:tws.{k}" for k in ("needs", "opens", "prompt")}
    assert named <= ids, sorted(named - ids)


@pytest.mark.parametrize("path", EXPORTS)
def test_fixture_exports_give_valid_entities_relations_and_known_constructs(path):
    out = fixture(path)
    assert out.parsed
    known = {c.id for c in PACK.constructs}
    assert constructs(out) and {cid for cid, _ in constructs(out)} <= known
    for o in out.observations:
        assert o.evidence_class in ("E1", "E4") or getattr(o.evidence_class, "value", None) in ("E1", "E4")
        if o.kind == "entity":
            assert check_platform_kind(o.payload["kind"], o.payload["attributes"]) is None
        if o.kind == "relation":
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p
        assert o.evidence.span.end_byte <= len((F35 / path).read_bytes())
        assert o.kind not in ("behavior", "transform")  # declarative definitions: no procedural logic, no data movement
    for needle in NEVER_COPIED:
        assert needle not in blob(out), (path, needle)


def test_output_is_deterministic():
    for path in EXPORTS:
        assert [o.row() for o in fixture(path).observations] == [o.row() for o in fixture(path).observations]


def test_formats_are_told_from_the_content():
    assert detect(INPUTS + "x.xml", '<?xml version="1.0"?>\n<DEFTABLE><FOLDER/></DEFTABLE>') == "controlm"
    assert detect(INPUTS + "x.xml", '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"/>') == "wintask"
    assert detect(INPUTS + "x.txt", "insert_job: A job_type: CMD\n") == "autosys"
    assert detect(INPUTS + "x.jil", "/* empty */\n") == "autosys"
    assert detect(INPUTS + "x.lst", " NAME    ID MEMBER\n") == "ca7"
    assert detect(INPUTS + "x.txt", "SCHEDULE WS#S\n:\nWS#J\nEND\n") == "tws"
    assert detect(INPUTS + "x.txt", "$JOBS\nWS#J SCRIPTNAME \"/a.sh\"\n") == "tws"
    assert detect(INPUTS + "cron.d/batch", "0 1 * * * root /a.sh\n") == "cron"
    assert detect(INPUTS + "notes.txt", "nothing to see\n") is None
    assert detect(INPUTS + "x.xml", "<project/>") is None
    out = run("nothing to see", INPUTS + "notes.txt")
    assert not out.parsed and "not a scheduler export" in messages(out)[0]


# ---------------------------------------------------------------------- commands


@pytest.mark.parametrize("command, scripts, programs", [
    ("/opt/batch/load.sh %%ODATE", ["/opt/batch/load.sh"], []),
    ("cd /opt/batch && ./scripts/run.sh --x 1 >> /var/log/x.log 2>&1", ["./scripts/run.sh"], []),
    ("nohup nice -n 10 /opt/a.sh > /dev/null 2>&1 &", ["/opt/a.sh"], []),
    ("FOO=bar BAZ=1 timeout 600 /opt/b.ksh", ["/opt/b.ksh"], []),
    ("sas /opt/sas/x.sas -log /tmp/x.log", ["/opt/sas/x.sas"], []),
    ("sas -sysin /opt/sas/y.sas", ["/opt/sas/y.sas"], []),
    ("python3 -m package.module --date today", [], ["python3"]),
    ("bash -c 'echo hi'", [], ["bash"]),
    ("perl -I /opt/lib /opt/bin/job.pl", ["/opt/bin/job.pl"], []),
    ('powershell.exe -NoProfile -File "C:\\Batch Jobs\\run.ps1" -Mode full', ["C:\\Batch Jobs\\run.ps1"], []),
    ('cmd.exe /c "D:\\batch\\nightly.bat" arg', ["D:\\batch\\nightly.bat"], []),
    ("sqlplus -s user/secret@db @/opt/sql/load.sql", ["/opt/sql/load.sql"], []),
    ("dsjob -run -jobstatus PROJ LoadOrders", [], ["dsjob"]),
    ("/opt/a.sh | mail -s done ops", ["/opt/a.sh"], ["mail"]),
    ("java -jar /opt/app/batch.jar --spring.profiles.active=prod", ["/opt/app/batch.jar"], []),
])
def test_what_a_command_starts(command, scripts, programs):
    s, p, _ = starts(command)
    assert s == scripts and p == programs


def test_a_command_with_a_variable_is_dynamic():
    assert starts("$HOME/bin/run.sh") == ([], [], ["$HOME/bin/run.sh"])
    assert starts("%BATCH%\\run.bat") == ([], [], ["%BATCH%\\run.bat"])
    assert starts("sh ${DIR}/x.sh")[2] == ["${DIR}/x.sh"]


def test_words_keep_windows_paths_and_split_operators():
    assert words(r'C:\a\b.bat "x y" z>>log&&q') == ["C:\\a\\b.bat", "x y", "z", ">>", "log", "&&", "q"]


def test_script_paths_become_every_suffix_and_never_keep_a_host():
    assert path_suffixes("/opt/batch/scripts/x.sh") == ["opt/batch/scripts/x.sh", "batch/scripts/x.sh", "scripts/x.sh", "x.sh"]
    assert path_suffixes("D:\\batch\\x.ps1") == ["batch/x.ps1", "x.ps1"]
    assert path_suffixes("\\\\fileserver01\\share\\jobs\\x.bat") == ["jobs/x.bat", "x.bat"]
    assert path_suffixes("../scripts/./x.sh") == ["scripts/x.sh", "x.sh"]
    assert path_suffixes("$HOME/x.sh") == [] and path_suffixes("~/x.sh") == [] and path_suffixes("/a/*.sh") == []
    out = run('<DEFTABLE><FOLDER FOLDER_NAME="F"><JOB JOBNAME="J" TASKTYPE="Command" CMDLINE="\\\\fileserver01\\share\\jobs\\x.bat"/></FOLDER></DEFTABLE>', INPUTS + "c.xml")
    assert "fileserver01" not in blob(out)
    assert entities(out)[job("control-m", "F/J")]["scripts"] == ["/jobs/x.bat"]


# ---------------------------------------------------------------------- Control-M


def test_control_m_folders_jobs_schedules_and_calendars():
    out = fixture(INPUTS + "controlm/orders.xml")
    ents = entities(out)
    folder = ents[job("control-m", "ORDERS_DAILY")]
    assert folder["platform_kind"] == "sched:ControlMFolder" and folder["kind"] == "smart_folder"
    assert folder["schedule"] == "WEEKDAYS=1,2,3,4,5 TIMEFROM=0100" and folder["calendar"] == "WORKDAYS" and folder["calendars"] == ["ORDER_DAYS", "WORKDAYS"]
    extract = ents[job("control-m", "ORDERS_DAILY/EXTRACT_ORDERS")]
    assert extract["calendars"] == ["HOLIDAYS", "WORKDAYS"] and extract["description"] == "Extract the day's orders" and extract["application"] == "ORDERS"
    assert ents[job("control-m", "ORDERS_DAILY/ORDERS_DONE")]["kind"] == "dummy"
    rels = relations(out)
    assert (job("control-m", "ORDERS_DAILY"), "CONTAINS", job("control-m", "ORDERS_DAILY/EXTRACT_ORDERS")) in rels
    assert (job("control-m", "PAYROLL_ZOS"), "CONTAINS", job("control-m", "PAYROLL_ZOS/PAYJ030")) in rels


def test_control_m_conditions_link_jobs_of_the_same_export_only():
    out = fixture(INPUTS + "controlm/orders.xml")
    rels = relations(out)
    dep = rels[(job("control-m", "ORDERS_DAILY/LOAD_ORDERS"), "DEPENDS_ON", job("control-m", "ORDERS_DAILY/EXTRACT_ORDERS"))]
    assert dep["condition"] == "EXTRACT_ORDERS-OK" and dep["_ev"] == "E1"
    done = entities(out)[job("control-m", "ORDERS_DAILY/ORDERS_DONE")]
    assert done["unresolved_conditions"] == ["FINANCE-READY"] and done["in_conditions"] == ["FINANCE-READY", "LOAD_ORDERS-OK"]
    assert any("FINANCE-READY" in m for m in messages(out))
    counts = constructs(out)
    assert counts[("sched:ctm.incond", True)] == 3 and counts[("sched:ctm.incond", False)] == 1
    assert counts[("sched:ctm.outcond-delete", False)] == 1  # SIGN="-" removes a condition: it makes no producer


def test_control_m_or_conditions_do_conditions_and_cyclic_jobs():
    xml = """<DEFTABLE>
  <FOLDER FOLDER_NAME="F" >
    <JOB JOBNAME="A" TASKTYPE="Dummy">
      <ON STMT="*" CODE="NOTOK"><DOCOND NAME="A-FAILED" ODATE="ODAT" SIGN="+"/><DOFORCEJOB TABLE_NAME="F" NAME="C"/></ON>
    </JOB>
    <JOB JOBNAME="B" TASKTYPE="Dummy" CYCLIC="1" CYCLIC_TYPE="Interval" INTERVAL="00010M" JAN="1" FEB="1">
      <INCOND NAME="A-FAILED" ODATE="PREV" AND_OR="O"/>
      <SHOUT WHEN="LATESUB" DEST="someone@example.invalid"/>
      <CONTROL NAME="LOCK" TYPE="E"/>
    </JOB>
    <SUB_FOLDER JOBNAME="SUB"><JOB JOBNAME="C" TASKTYPE="Dummy"/></SUB_FOLDER>
  </FOLDER>
</DEFTABLE>"""
    out = run(xml, INPUTS + "c.xml")
    rels = relations(out)
    assert rels[(job("control-m", "F/B"), "DEPENDS_ON", job("control-m", "F/A"))] == {"condition": "A-FAILED", "logic": "or", "odate": "PREV", "_ev": "E1"}
    ents = entities(out)
    assert ents[job("control-m", "F/B")]["schedule"] == "MONTHS=JAN,FEB CYCLIC=1 CYCLIC_TYPE=Interval INTERVAL=00010M"
    assert (job("control-m", "F/SUB"), "CONTAINS", job("control-m", "F/SUB/C")) in rels and (job("control-m", "F"), "CONTAINS", job("control-m", "F/SUB")) in rels
    counts = constructs(out)
    for cid in ("sched:ctm.on-do", "sched:ctm.doforcejob", "sched:ctm.shout", "sched:ctm.control"):
        assert counts[(cid, False)] == 1, cid
    assert counts[("sched:ctm.cyclic", True)] == 1
    assert "example.invalid" not in blob(out)


@pytest.mark.parametrize("script,platform", [
    ("run.sh", "shell"), ("run.ksh", "shell"), ("run.bash", "shell"), ("run.bat", "shell"), ("run.cmd", "shell"), ("run.ps1", "shell"),
    ("run.hql", "hive"), ("run.q", "hive"), ("run.pig", "pig"), ("run.sas", "sas"), ("run.py", None), ("run.sql", None),
])
def test_scripts_also_run_the_program_their_pack_declares(script, platform):
    from repo_intel.platforms.scheduler.common import Emitter

    text = "x"
    em = Emitter("scheduler.exports", INPUTS + "cron/x.crontab", text.encode(), "b" * 64, text, "utf-8", 0)
    me = {"kind": "Job", "natural_key": {"framework": "cron", "name": "j"}, "name": "j"}
    assert em.script(em.whole(), me, f"/opt/jobs/{script}")
    rels = [o.payload for o in em.items if o.kind == "relation"]
    programs = [r["object"] for r in rels if r["object"]["kind"] == "Program"]
    if platform is None:
        assert not programs
        return
    assert [p["natural_key"] for p in programs] == [{"platform": platform, "name": n} for n in (f"opt/jobs/{script}", f"jobs/{script}", script)]
    assert all(p["declare"] is False for p in programs)
    assert all(o.evidence_class == "E4" for o in em.items if o.kind == "relation")


def test_control_m_runs_jcl_members_scripts_and_never_a_dynamic_member():
    out = fixture(INPUTS + "controlm/orders.xml")
    rels = relations(out)
    jcl = rels[(job("control-m", "PAYROLL_ZOS/PAYJ010"), "RUNS", job("jcl", "PAYJ010"))]
    assert jcl == {"via": "jcl_member", "_ev": "E4"}  # a member's JOB statement carries its name only by convention
    for suffix in ("opt/batch/scripts/extract_orders.sh", "batch/scripts/extract_orders.sh", "scripts/extract_orders.sh", "extract_orders.sh"):
        assert rels[(job("control-m", "ORDERS_DAILY/EXTRACT_ORDERS"), "RUNS", art(suffix))] == {"via": "path_suffix", "_ev": "E4"}
        # and the shell pack's Program for the same candidate path
        assert rels[(job("control-m", "ORDERS_DAILY/EXTRACT_ORDERS"), "RUNS", prog("shell", suffix))] == {"via": "path_suffix", "_ev": "E4"}
    assert (job("control-m", "ORDERS_DAILY/LOAD_ORDERS"), "RUNS", art("scripts/load_orders.sh")) in rels
    assert not [r for r in rels if r[0] == job("control-m", "PAYROLL_ZOS/PAYJ030") and r[1] == "RUNS"]
    assert entities(out)[job("control-m", "PAYROLL_ZOS/PAYJ030")]["runs_dynamic"] is True
    assert any("AutoEdit" in m for m in messages(out))
    for o in out.observations:
        if o.kind == "relation" and o.payload["predicate"] == "RUNS":
            assert o.payload["object"]["declare"] is False


def test_control_m_xml_with_a_doctype_or_malformed_is_refused():
    out = run('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "x">]><DEFTABLE/>', INPUTS + "c.xml")
    assert not out.parsed and "DOCTYPE" in messages(out)[0]
    out = run("<DEFTABLE><FOLDER></DEFTABLE>", INPUTS + "c.xml")
    assert not out.parsed and "well-formed" in messages(out)[0]


def test_control_m_duplicate_job_names_are_numbered():
    out = run('<DEFTABLE><FOLDER FOLDER_NAME="F"><JOB JOBNAME="J" TASKTYPE="Dummy"/><JOB JOBNAME="J" TASKTYPE="Dummy"/></FOLDER></DEFTABLE>', INPUTS + "c.xml")
    assert {job("control-m", "F/J"), job("control-m", "F/J~2")} <= set(entities(out))


# ---------------------------------------------------------------------- AutoSys


def test_autosys_boxes_jobs_schedules_and_calendars():
    out = fixture(INPUTS + "autosys/reports.jil")
    ents = entities(out)
    box = ents[job("autosys", "RPT_NIGHTLY_BOX")]
    assert box["platform_kind"] == "sched:AutosysBox" and box["schedule"] == "days_of_week=mo,tu,we,th,fr start_times=03:00"
    assert box["calendar"] == "RPT_BUSINESS_DAYS" and box["exclude_calendar"] == "RPT_HOLIDAYS" and box["calendars"] == ["RPT_BUSINESS_DAYS", "RPT_HOLIDAYS"]
    rels = relations(out)
    for member in ("RPT_EXTRACT", "RPT_BUILD", "RPT_REFRESH_CICS"):
        assert (job("autosys", "RPT_NIGHTLY_BOX"), "CONTAINS", job("autosys", member)) in rels
    assert job("autosys", "RPT_EXTRACT~2") not in ents  # update_job is counted, not a second definition
    assert constructs(out)[("sched:jil.other-statement", False)] == 1


def test_autosys_conditions():
    out = fixture(INPUTS + "autosys/reports.jil")
    rels = relations(out)
    build = job("autosys", "RPT_BUILD")
    assert rels[(build, "DEPENDS_ON", job("autosys", "RPT_EXTRACT"))] == {"on": "success", "_ev": "E1"}
    assert rels[(build, "DEPENDS_ON", job("autosys", "ORD_FEED"))] == {"on": "success", "optional": True, "_ev": "E1"}
    assert rels[(build, "DEPENDS_ON", job("autosys", "ORD_FEED_FALLBACK"))] == {"on": "done", "optional": True, "_ev": "E1"}
    assert (job("autosys", "RPT_REFRESH_CICS"), "DEPENDS_ON", job("autosys", "RPT_BUILD")) in rels
    assert entities(out)[job("autosys", "PAY_SUBMIT_J020")]["external_conditions"] == ["PAYJ010^CA7"]
    counts = constructs(out)
    assert counts[("sched:jil.global-variable", False)] == 1 and counts[("sched:jil.external-condition", False)] == 1


@pytest.mark.parametrize("text, expected", [
    ("s(A)", [("success", "A", None, True)]),
    ("s(A) & f(B)", [("success", "A", None, True), ("failure", "B", None, True)]),
    ("s(A) | f(B)", [("success", "A", None, False), ("failure", "B", None, False)]),
    ("s(A) and (d(B) or t(C)) & n(D)", [("success", "A", None, True), ("done", "B", None, False), ("terminated", "C", None, False), ("notrunning", "D", None, True)]),
    ("success(A, 01.30) & exitcode(B) > 4", [("success", "A", "01.30", True), ("exitcode", "B", None, True)]),
    ("v(MODE) = \"x\" & s(A^PRD)", [("variable", "MODE", None, True), ("success", "A^PRD", None, True)]),
])
def test_autosys_condition_parser(text, expected):
    got = [(status, name, look, required) for status, name, look, _, _, required in autosys._parse_condition(text)]
    assert got == expected


@pytest.mark.parametrize("text", ["s(A", "s(A) &", "x(A)", "s(A) s(B)", ""])
def test_autosys_condition_that_does_not_parse_is_not_guessed(text):
    assert autosys._parse_condition(text) is None
    out = run(f"insert_job: J job_type: CMD\ncommand: /a.sh\ncondition: {text}\n", INPUTS + "a.jil") if text else None
    if out:
        assert not [r for r in relations(out) if r[1] == "DEPENDS_ON"] and any("does not parse" in m for m in messages(out))


def test_autosys_job_types_cics_zos_file_watcher_and_others():
    out = fixture(INPUTS + "autosys/reports.jil")
    ents = entities(out)
    ep = key("Endpoint", component_root="@unit", protocol="cics", method="TRANSACTION", route="RPT1")
    assert ents[ep]["platform_kind"] == "sched:EntryPoint" and ents[ep]["fidelity"] == "lossy" and ents[ep]["job"] == "RPT_REFRESH_CICS"
    assert ents[job("autosys", "RPT_REFRESH_CICS")]["transaction"] == "RPT1"
    rels = relations(out)
    assert rels[(job("autosys", "PAY_SUBMIT_J020"), "RUNS", job("jcl", "PAYJ020"))] == {"via": "jcl_member", "_ev": "E4"}
    assert ents[job("autosys", "ORD_FEED")]["watch_file"] == "/data/inbound/orders/feed_ready.flg"
    sap = run("insert_job: S job_type: SAP\nsap_job_name: ZREPORT\n", INPUTS + "a.jil")
    assert constructs(sap)[("sched:jil.other-job-type", False)] == 1 and entities(sap)[job("autosys", "S")]["kind"] == "sap"


def test_autosys_schedule_needs_date_conditions():
    out = run("insert_job: J job_type: c\ncommand: /a.sh\ndays_of_week: all\nstart_times: \"01:00\"\n", INPUTS + "a.jil")
    assert "schedule" not in entities(out)[job("autosys", "J")]
    assert constructs(out)[("sched:jil.start-times", False)] == 2


def test_autosys_comments_and_names():
    text = "/* insert_job: NOT_A_JOB */\n# insert_job: NOR_THIS\ninsert_job: REAL   job_type: c\ncommand: /a.sh\n"
    assert set(entities(run(text, INPUTS + "a.jil"))) == {job("autosys", "REAL")}


# ---------------------------------------------------------------------- IBM Workload Scheduler


def test_tws_jobs_streams_schedules_and_calendars():
    out = fixture(INPUTS + "tws/finance.txt")
    ents = entities(out)
    assert ents[job("tws", "FINWS#GL_EXTRACT")] == {**ents[job("tws", "FINWS#GL_EXTRACT")], "kind": "script", "task_type": "UNIX", "scripts": ["/opt/fin/bin/gl_extract.sh"]}
    daily = ents[job("tws", "FINWS#FIN_DAILY")]
    assert daily["schedule"] == 'ON RUNCYCLE DAILY1 "FREQ=DAILY;INTERVAL=1"; EXCEPT HOLIDAYS; AT 0400'
    assert daily["exclude_calendar"] == "HOLIDAYS" and "calendar" not in daily  # DAILY1 names the run cycle, not a calendar
    assert ents[job("tws", "FINWS#FIN_MONTHEND")]["calendar"] == "MONTHEND"
    assert job("tws", "FINWS#GL_RESET") not in ents
    counts = constructs(out)
    assert counts[("sched:tws.other-definition", False)] == 1 and counts[("sched:tws.needs", False)] == 1 and counts[("sched:tws.recovery", False)] == 2


def test_tws_stream_jobs_refer_to_definitions_unless_defined_in_the_stream():
    out = fixture(INPUTS + "tws/finance.txt")
    payloads = {(ref(o.payload["subject"]), ref(o.payload["object"])): o.payload["object"] for o in out.observations if o.kind == "relation" and o.payload["predicate"] == "CONTAINS"}
    assert payloads[(job("tws", "FINWS#FIN_DAILY"), job("tws", "FINWS#GL_POST"))]["declare"] is False
    assert payloads[(job("tws", "FINWS#FIN_MONTHEND"), job("tws", "FINWS#GL_CLOSE"))].get("declare", True) is True


def test_tws_follows():
    out = fixture(INPUTS + "tws/finance.txt")
    rels = relations(out)
    assert rels[(job("tws", "FINWS#GL_POST"), "DEPENDS_ON", job("tws", "FINWS#GL_EXTRACT"))] == {"stream": "FINWS#FIN_DAILY", "_ev": "E1"}
    assert rels[(job("tws", "FINWS#GL_CLOSE"), "DEPENDS_ON", job("tws", "FINWS#GL_POST"))] == {"stream": "FINWS#FIN_MONTHEND", "_ev": "E1"}
    assert (job("tws", "FINWS#FIN_DAILY"), "DEPENDS_ON", job("tws", "CTMWS#ORDERS_FEED")) in rels


def test_tws_follows_aliases_streams_elsewhere_and_other_networks():
    text = """SCHEDULE WS1#S1
:
WS1#LOAD AS L1
WS1#REPORT
 FOLLOWS L1, WS2#OTHER.EXTRACT
 FOLLOWS NET1::WS3#FAR.JOB
END
"""
    out = run(text, INPUTS + "s.txt")
    rels = relations(out)
    assert (job("tws", "WS1#REPORT"), "DEPENDS_ON", job("tws", "WS1#LOAD")) in rels
    # a job of a stream not in this export: the dependency is on that stream, the job kept as a qualifier
    assert rels[(job("tws", "WS1#REPORT"), "DEPENDS_ON", job("tws", "WS2#OTHER"))] == {"stream": "WS1#S1", "job": "EXTRACT", "_ev": "E1"}
    assert constructs(out)[("sched:tws.follows", False)] == 1 and any("another network" in m for m in messages(out))


# ---------------------------------------------------------------------- CA-7


def test_ca7_jobs_requirements_triggers_and_schedules():
    out = fixture(INPUTS + "ca7/payroll.lst")
    ents = entities(out)
    pay10 = ents[job("ca7", "PAYJ010")]
    assert pay10["jcl_member"] == "PAYJ010" and pay10["application"] == "PAYROLL" and pay10["calendar"] == "PY"
    assert pay10["schedule"] == "SCHID=001 ROLL=D INDEX=+000 SCAL=PY DOTM=2300 LEADTM=0030 STARTM=2230 WEEKLY DAYS=MON,TUE,WED,THU,FRI"
    assert ents[job("ca7", "PAYJ020")]["requires_datasets"] == ["PAY.MASTER.FILE"]
    rels = relations(out)
    assert rels[(job("ca7", "PAYJ010"), "RUNS", job("jcl", "PAYJ010"))] == {"_ev": "E1"}  # CA-7 job names are the JOB statement's
    assert rels[(job("ca7", "PAYJ020"), "DEPENDS_ON", job("ca7", "PAYJ010"))]["_ev"] == "E1"
    assert constructs(out)[("sched:ca7.user-requirement", False)] == 1


def test_ca7_listing_in_another_layout_gives_nothing():
    out = run("PAYJ010 runs after PAYJ005\n", INPUTS + "ca7/notes.lst")
    assert not entities(out)


def test_ca7_triggered_by():
    text = " NAME    ID MEMBER   -NAME-\n\n A1 000 A1M SYS\n ---- TRIGGERED BY ----\n JOB=A0\n"
    out = run(text, INPUTS + "x.lst")
    assert (job("ca7", "A1"), "DEPENDS_ON", job("ca7", "A0")) in relations(out)
    assert entities(out)[job("ca7", "A1")]["jcl_member"] == "A1M"


# ---------------------------------------------------------------------- cron


def test_cron_entries():
    out = fixture(INPUTS + "cron/batch.crontab")
    ents = entities(out)
    path = INPUTS + "cron/batch.crontab"
    assert ents[job("cron", f"{path}#purge_logs.sh")]["schedule"] == "15 1 * * *"
    assert ents[job("cron", f"{path}#queue_listener")]["schedule"] == "@reboot"
    assert ents[job("cron", f"{path}#entry")]["runs_dynamic"] is True
    rels = relations(out)
    assert (job("cron", f"{path}#load_orders.sh"), "RUNS", art("scripts/load_orders.sh")) in rels
    assert constructs(out)[("sched:cron.variable", False)] == 2


def test_cron_system_format_percent_and_repeated_names():
    text = "0 1 * * * root /opt/a.sh\n0 2 * * * batch /opt/a.sh % secret input\n*/5 * * * * nobody echo hi\n"
    out = run(text, INPUTS + "cron.d/jobs")
    ents = entities(out)
    assert {job("cron", INPUTS + "cron.d/jobs#a.sh"), job("cron", INPUTS + "cron.d/jobs#a.sh~2"), job("cron", INPUTS + "cron.d/jobs#entry")} == set(ents)
    text_out = blob(out)
    assert "root" not in text_out and "nobody" not in text_out and "secret input" not in text_out
    user = run("0 1 * * * /opt/a.sh\n", INPUTS + "cron/batch.crontab")
    assert (job("cron", INPUTS + "cron/batch.crontab#a.sh"), "RUNS", art("a.sh")) in relations(user)


# ---------------------------------------------------------------------- Windows Task Scheduler


def test_windows_task_in_utf16_with_spans_on_its_bytes():
    path = INPUTS + "windows/NightlyInvoices.xml"
    data = (F35 / path).read_bytes()
    assert data.startswith(b"\xff\xfe")
    out = fixture(path)
    ents = entities(out)
    task = ents[job("windows-task", "\\Finance\\NightlyInvoices")]
    assert task["schedule"] == "CalendarTrigger StartBoundary=2026-01-05T02:30:00 ScheduleByWeek WeeksInterval=1 DaysOfWeek=Monday,Tuesday,Wednesday,Thursday,Friday"
    assert task["scripts"] == ["D:/batch/scripts/windows/invoices.ps1"]
    assert (job("windows-task", "\\Finance\\NightlyInvoices"), "RUNS", art("scripts/windows/invoices.ps1")) in relations(out)
    entity = next(o for o in out.observations if o.kind == "entity")
    span = entity.evidence.span
    assert data[span.start_byte:span.end_byte].decode("utf-16-le") == "<URI>\\Finance\\NightlyInvoices</URI>"
    exec_span = next(o for o in out.observations if o.kind == "relation").evidence.span
    assert data[exec_span.start_byte:exec_span.end_byte].decode("utf-16-le") == "<Exec"
    assert constructs(out)[("sched:win.principal", False)] == 1


def test_windows_task_without_a_uri_and_other_actions():
    xml = """<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><BootTrigger/><EventTrigger><Subscription>&lt;QueryList&gt;secret-query&lt;/QueryList&gt;</Subscription></EventTrigger></Triggers>
  <Actions><ComHandler><ClassId>{0000}</ClassId></ComHandler><Exec><Command>C:\\tools\\run.cmd</Command></Exec></Actions>
</Task>"""
    out = run(xml, INPUTS + "windows/Cleanup.xml")
    ents = entities(out)
    assert ents[job("windows-task", "\\Cleanup")]["schedule"] == "BootTrigger; EventTrigger"
    assert "secret-query" not in blob(out)
    counts = constructs(out)
    assert counts[("sched:win.com-handler", False)] == 1 and counts[("sched:win.event-trigger", True)] == 1
    assert (job("windows-task", "\\Cleanup"), "RUNS", art("tools/run.cmd")) in relations(out)


# ---------------------------------------------------------------------- the fixture, compiled


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    from repo_intel.compile.fixtures import materialize
    from repo_intel.compile.pipeline import compile_commit, load_shards
    from repo_intel.storage.store import ObjectStore

    root = tmp_path_factory.mktemp("F35")
    sha = materialize("F35-scheduler-exports", root / "repo")[0]
    store = ObjectStore(root / "store", "t")
    result = compile_commit(store, root / "repo", "github.com/1001", sha)
    shards = load_shards(store, result.manifest)
    return {"entities": {e["id"]: e for s in shards.values() for e in s["entities"]}, "assertions": [a for s in shards.values() for a in s["assertions"]],
            "coverage": store.get_json(result.manifest.coverage_ref)}


def test_runs_links_only_to_what_the_repository_holds(compiled):
    ents = compiled["entities"]
    runs = [(ents[a["subject"]], ents[a["object"]]) for a in compiled["assertions"] if a["predicate"] == "RUNS" and ents[a["subject"]]["attributes"].get("platform") == "scheduler"]

    def target(o):
        nk = o["natural_key"]
        if o["kind"] == "Artifact":
            return nk["path"]
        return f"{nk.get('framework') or nk.get('platform')}:{nk['name']}"

    targets = sorted({target(o) for _, o in runs})
    scripts = ["fin/bin/gl_extract.sh", "scripts/extract_orders.sh", "scripts/load_orders.sh", "scripts/purge_logs.sh", "scripts/windows/invoices.ps1"]
    # Each script is linked as its file and as the Program the shell pack declares for it.
    assert targets == sorted(["jcl:PAYJ010", "jcl:PAYJ020", *scripts, *[f"shell:{p}" for p in scripts]])
    # no job runs something the repository does not hold: gl_post.sh, build_reports.sas and queue_listener are not linked
    assert {s["name"] for s, _ in runs} >= {"RPT_EXTRACT", "FINWS#GL_EXTRACT", "PAYJ010"}
    assert not [s for s, _ in runs if s["name"] in ("RPT_BUILD", "FINWS#GL_POST")]


def test_coverage_of_scheduler_constructs(compiled):
    [cov] = [c for c in compiled["coverage"] if c["domain"] == "platform_constructs" and c["scope"] == "scheduler"]
    assert (cov["numerator"], cov["denominator"]) == (85, 117)
    assert "sched:ctm.application-type" in cov["detail"]["not_seen"]


def test_names_and_never_values_in_the_compiled_fixture(compiled):
    text = json.dumps([compiled["entities"], compiled["assertions"]], sort_keys=True)
    for needle in NEVER_COPIED:
        assert needle not in text, needle
