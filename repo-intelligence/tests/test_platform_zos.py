"""The z/OS platform pack (repo_intel/platforms/zos): the JCL reader (statements, procedures,
symbolics, overrides, referbacks), control cards (IDCAMS, DFSORT, ICETOOL, TSO), CICS CSD
exports, DCLGEN, BIND cards and DB2 DDL, the superset of legacy.jcl, and the linker pass
that joins a program's DD names to the data sets its JCL binds (`link_dd_datasets`)."""

import ast
import hashlib
import json
import time
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile import behavior, transforms
from repo_intel.compile.ontology import EntityKind, check_relation
from repo_intel.extractors import legacy
from repo_intel.platforms import packs, superseded, validate_pack
from repo_intel.platforms.zos import PACK, ZosDefinitions, ZosJcl, cards, csd, db2, jcl, source

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "benchmarks" / "fixtures"
F38 = FIXTURES / "F38-zos-definitions" / "base"


def run(text: str, path: str = "jcl/MEMBER1.jcl", extractor=None):
    data = text.encode()
    ex = extractor or (ZosDefinitions() if ZosDefinitions().matches(path) else ZosJcl())
    return ex.extract(path, data, hashlib.sha256(data).hexdigest())


def ref(x) -> tuple:
    return (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def entities(out) -> dict[tuple, dict]:
    found: dict[tuple, dict] = {}
    for o in out.observations:
        if o.kind == "entity":
            found.setdefault(ref(o.payload), {}).update(o.payload["attributes"])
    return found


def relations(out) -> set[tuple]:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])) for o in out.observations if o.kind == "relation"}


def qualified(out) -> list[tuple]:
    return [(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"]), o.payload.get("qualifiers") or {}) for o in out.observations if o.kind == "relation"]


def constructs(out) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = {}
    for o in out.observations:
        if o.kind == "construct":
            key = (o.payload["construct"], o.payload["modelled"])
            counts[key] = counts.get(key, 0) + 1
    return counts


def of(out, kind: str) -> list[dict]:
    return [o.payload for o in out.observations if o.kind == kind]


def job(name):
    return ("Job", json.dumps({"framework": "jcl", "name": name}, sort_keys=True))


def task(j, name):
    return ("Task", json.dumps({"framework": "jcl", "job": j, "name": name}, sort_keys=True))


def ds(name):
    return ("Dataset", json.dumps({"name": name}))


def prog(name, platform="mainframe"):
    return ("Program", json.dumps({"name": name, "platform": platform}, sort_keys=True))


def endpoint(route):
    return ("Endpoint", json.dumps({"component_root": "@unit", "method": "TRANSACTION", "protocol": "cics", "route": route}, sort_keys=True))


def blob_of(out) -> str:
    return json.dumps([o.payload for o in out.observations], sort_keys=True, default=str) + json.dumps([d.message for d in out.diagnostics])


# ---------------------------------------------------------------------- the pack


def test_pack_is_registered_valid_and_supersedes_legacy_jcl():
    assert packs()["zos"] is PACK
    assert validate_pack(PACK) == []
    assert PACK.prefix == "zos" and PACK.version == "1.0.0"
    assert PACK.supersedes == ("legacy.jcl",) and "legacy.jcl" in superseded()
    assert {e.id for e in PACK.extractors} == {"zos.jcl", "zos.definitions"}
    assert PACK.inputs == (".rkp/inputs/zos/**",)
    assert len(PACK.constructs) >= 90


def test_pack_modules_import_extractors_only_inside_functions():
    for path in (ROOT / "repo_intel" / "platforms" / "zos").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(n.startswith("repo_intel.extractors") for n in names), f"{path.name} imports {names} at module level"


def test_constructs_the_readers_name_are_listed():
    ids = {c.id for c in PACK.constructs}
    assert {f"zos:{c}" for c in csd.RESOURCES.values()} <= ids
    assert {f"zos:{k}" for k, _, _ in [("jes2", 0, 0), ("jes3", 0, 0), ("scheduler_directive", 0, 0)]} <= ids
    assert {f"zos:{c}" for c in ("ddl_table", "ddl_view", "ddl_index", "ddl_storage", "ddl_alias", "ddl_grant", "ddl_comment", "ddl_other")} <= ids
    seen = set()
    for out in [run(SAMPLE), run(SAMPLE_UTILITIES, "jcl/PAYROLL.jcl")] + [run(p.read_text(), str(p.relative_to(F38))) for p in sorted(F38.rglob("*")) if p.is_file() and not p.name.startswith(("_", "README"))]:
        seen |= {c for c, _ in constructs(out)}
    assert seen <= ids, sorted(seen - ids)


def test_terms_are_lossy_and_name_core_kinds():
    used = {"zos:Job", "zos:Proc", "zos:Member", "zos:Step", "zos:Transaction", "zos:Dclgen", "zos:DclgenField", "zos:Db2Package", "zos:Db2Plan"}
    assert {t.name for t in PACK.terms} == used
    assert all(t.fidelity == "lossy" for t in PACK.terms)


def test_relations_the_pack_emits_are_allowed_by_the_ontology():
    outs = [run(SAMPLE), run(SAMPLE_UTILITIES, "jcl/PAYROLL.jcl"), run(CSD, ".rkp/inputs/zos/ACCT.csd"), run(BIND, ".rkp/inputs/zos/A.bind"), run(DCLGEN, ".rkp/inputs/zos/DACCT.dclgen")]
    for out in outs:
        for s, p, o in relations(out):
            assert check_relation(p, EntityKind(s[0]), EntityKind(o[0])) is None, (s, p, o)


# ---------------------------------------------------------------------- superset of legacy.jcl


def _keys(out) -> set[tuple]:
    def plain(r):
        return (r["kind"], json.dumps(r["natural_key"], sort_keys=True))

    keys = set()
    for o in out.observations:
        if o.kind == "entity":
            keys.add(("E", *plain(o.payload)))
        elif o.kind == "relation":
            keys.add(("R", plain(o.payload["subject"]), o.payload["predicate"], plain(o.payload["object"])))
    return keys


LEGACY_SNIPPETS = [
    "//J1 JOB\n//S1 EXEC PGM=P1\n//IN DD DSN=A.B.C,DISP=SHR\n//OUT DD DSN=A.B.D(+1),DISP=(NEW,CATLG)\n",
    "//J2 JOB\n//EXEC PGM=IEFBR14\n//DD1 DD DSN=X.Y,DISP=(MOD,DELETE)\n// EXEC PGM=P2\n//STEPLIB DD DSN=X.LOAD,DISP=SHR\n//I DD DSN=&&T,DISP=SHR\n",
    "//PR1 PROC\n//S1 EXEC PGM=P3\n//I DD DSN=&HLQ..X,DISP=SHR\n//O DD DSN=SYS1.PROCLIB,DISP=SHR\n// PEND\n",
    "//J3 JOB\n//S1 EXEC PROC=MYPROC\n//S2 EXEC MYPROC2\n//S3 EXEC PGM=IKJEFT01\n//SYSTSIN DD *\n RUN PROGRAM(CUSTUPD) PLAN(P)\n/*\n//S4 EXEC PGM=*.S1.X\n",
    "//J4 JOB\n//S1 EXEC PGM=P1\n//O DD DSN=A.B,DISP=\n//O2 DD DSN=A.C,DISP=(,CATLG)\n//O3 DD DSN=A.D\n",
]


@pytest.mark.parametrize("path", [
    "F14-mainframe-sas/base/jcl/CUSTJOB.jcl", "F14-mainframe-sas/commits/01-report-step/jcl/CUSTJOB.jcl", "F28-oracle-plsql/base/jcl/NIGHTLY.prc",
    "F28-oracle-plsql/base/db/procedures/purge_stale_carts.prc", "F35-scheduler-exports/base/jcl/PAYJ010.jcl", "F35-scheduler-exports/base/jcl/PAYJ020.jcl",
    "F38-zos-definitions/base/jcl/ACCTARCH.prc",
])
def test_superset_of_legacy_jcl_on_fixtures(path):
    data = (FIXTURES / path).read_bytes()
    rel = path.split("/", 2)[2].split("/", 1)[1] if "/commits/" in path else path.split("/base/")[1]
    old = legacy.Jcl().extract(rel, data, "b")
    new = ZosJcl().extract(rel, data, "b")
    assert _keys(old) <= _keys(new), sorted(_keys(old) - _keys(new))
    _same_attributes(old, new)



def test_in_stream_procedure_steps_are_the_one_departure_from_legacy_jcl():
    """legacy.jcl keyed every EXEC of a file under its first JOB, so the steps of an in-stream
    procedure became steps of the job, chained into its order. Here they belong to the
    procedure (Task {job: <proc>}), which the job CONTAINS; the calling step carries their
    data sets. Everything else legacy.jcl emits on the file is emitted with the same keys."""
    data = (F38 / "jcl/ACCTDLY.jcl").read_bytes()
    old, new = legacy.Jcl().extract("jcl/ACCTDLY.jcl", data, "b"), ZosJcl().extract("jcl/ACCTDLY.jcl", data, "b")
    missing = _keys(old) - _keys(new)
    proc_step = ("Task", json.dumps({"framework": "jcl", "job": "ACCTD01", "name": "EXTR"}, sort_keys=True))
    assert missing and all(m[1:] == proc_step or proc_step in m for m in missing), sorted(missing)
    assert ("E", *task("EXTRACT", "EXTR")) in _keys(new)

@pytest.mark.parametrize("text", LEGACY_SNIPPETS)
def test_superset_of_legacy_jcl_on_snippets(text):
    old, new = legacy.Jcl().extract("jcl/X1.jcl", text.encode(), "b"), run(text, "jcl/X1.jcl")
    assert _keys(old) <= _keys(new), sorted(_keys(old) - _keys(new))
    _same_attributes(old, new)


def _same_attributes(old, new):
    got = entities(new)
    for o in old.observations:
        if o.kind == "entity":
            for k, v in o.payload["attributes"].items():
                assert got[ref(o.payload)].get(k) == v, (ref(o.payload), k, v)


def test_system_program_and_library_lists_keep_legacy_ones():
    assert legacy._SYSTEM_PROGRAMS <= source.SYSTEM_PROGRAMS
    assert source.LEGACY_SYSTEM_PROGRAMS == frozenset(legacy._SYSTEM_PROGRAMS)
    assert source.LIBRARY_DDS == frozenset(legacy._LIBRARY_DDS)


def test_a_prc_file_without_jcl_gives_nothing():
    out = run("CREATE OR REPLACE PROCEDURE purge AS\nBEGIN\n  NULL;\nEND;\n/\n", "db/purge.prc")
    assert [o for o in out.observations if o.kind in ("entity", "relation")] == []
    assert any("no JOB or PROC" in d.message for d in out.diagnostics)


# ---------------------------------------------------------------------- statements


def test_statements_continuations_quotes_comments_and_columns():
    head = "//S1       EXEC PGM=P1,PARM='ONE TWO,THREE"
    text = (
        "//J1       JOB (ACCT,'A,B'),'PGMR NAME',CLASS=A,          COMMENT TEXT\n"
        "//*  A COMMENT LINE\n"
        "//            MSGCLASS=X\n"
        + head.ljust(71) + "X" + "SEQ00001\n"  # a quoted value runs to column 71; 72 marks it; 73-80 are sequence numbers
        "//             FOUR'\n"
    )
    stmts, _ = jcl.statements(text)
    assert [s.op for s in stmts] == ["JOB", "EXEC"]
    keys = [p.key for p in jcl.split_params(stmts[0])]
    assert keys == [None, None, "CLASS", "MSGCLASS"]
    parm = stmts[1].param("PARM")
    value = jcl.unquote(parm.value)
    assert value.startswith("ONE TWO,THREE") and value.endswith("FOUR") and "X" not in value
    assert "SEQ" not in parm.value and "COMMENT" not in stmts[0].operands


def test_in_stream_data_star_data_and_delimiters():
    text = "//J1 JOB\n//S1 EXEC PGM=P1\n//A DD *\n LINE 1\n/*\n//B DD DATA,DLM=$$\n//NOT A STATEMENT\n$$\n//C DD *\n LINE 2\n//D DD DSN=X.Y,DISP=SHR\n"
    stmts, _ = jcl.statements(text)
    dds = [s for s in stmts if s.op == "DD"]
    assert [s.name for s in dds] == ["A", "B", "C", "D"]
    assert [ln for _, ln in dds[0].data.lines] == [" LINE 1"]
    assert [ln for _, ln in dds[1].data.lines] == ["//NOT A STATEMENT"]
    assert [ln for _, ln in dds[2].data.lines] == [" LINE 2"]  # DD * also ends at the next // statement


def test_implicit_sysin_and_jes_and_scheduler_statements():
    text = "//J1 JOB\n/*JOBPARM S=ANY\n//*%OPC SCAN\n//S1 EXEC PGM=SORT\n  SORT FIELDS=COPY\n//SORTIN DD DSN=A.B,DISP=SHR\n"
    out = run(text)
    c = constructs(out)
    assert c[("zos:jes2", False)] == 1 and c[("zos:scheduler_directive", False)] == 1
    assert c[("zos:dd_implicit_sysin", True)] == 1


def test_if_then_else_endif_across_lines_and_nested():
    text = (
        "//J1 JOB\n//S1 EXEC PGM=P1\n"
        "//  IF (S1.RC = 0 & S1.RC <= 4) |\n//     NOT S1.ABEND THEN\n"
        "//S2 EXEC PGM=P2\n//  IF RC > 8 THEN\n//S3 EXEC PGM=P3\n//  ENDIF\n"
        "//  ELSE\n//S4 EXEC PGM=P4\n//  ENDIF\n"
    )
    parsed = jcl.parse(text, "M")
    [u] = parsed.units
    s2, s3, s4 = u.steps[1:]
    assert s2.conditions == [{"if": "(S1.RC = 0 & S1.RC <= 4) | NOT S1.ABEND", "branch": "then"}] or s2.conditions[0]["branch"] == "then"
    assert len(s3.conditions) == 2 and s4.conditions[0]["branch"] == "else"
    [unit] = of(run(text), "behavior")
    assert behavior.validate_unit(unit) == []
    node = unit["body"][1]
    assert node["op"] == "if" and node["cond"]["op"] == "or" and node["cond"]["right"] == {"e": "unop", "op": "not", "operand": {"e": "member", "object": {"e": "var", "scope": "step", "name": "S1"}, "name": "ABEND"}}
    assert node["then"][1]["op"] == "if" and node["else"][0]["op"] == "call"


@pytest.mark.parametrize("text, want", [
    ("RC = 0", {"e": "binop", "op": "==", "left": {"e": "var", "scope": "job", "name": "RC"}, "right": {"e": "lit", "value": 0, "type": "number"}}),
    ("(STEP1.RC GT 4)", {"e": "binop", "op": ">", "left": {"e": "member", "object": {"e": "var", "scope": "step", "name": "STEP1"}, "name": "RC"}, "right": {"e": "lit", "value": 4, "type": "number"}}),
    ("S1.PS.RC NG 8", {"e": "binop", "op": "<=", "left": {"e": "member", "object": {"e": "member", "object": {"e": "var", "scope": "step", "name": "S1"}, "name": "PS"}, "name": "RC"}, "right": {"e": "lit", "value": 8, "type": "number"}}),
    ("ABEND", {"e": "var", "scope": "job", "name": "ABEND"}),
    ("S1.ABENDCC = U0100", {"e": "binop", "op": "==", "left": {"e": "member", "object": {"e": "var", "scope": "step", "name": "S1"}, "name": "ABENDCC"}, "right": {"e": "lit", "value": "U0100", "type": "string"}}),
    ("RC = = 0", {"e": "platform", "construct": "zos:if", "text": "RC = = 0"}),
    ("(RC = 0", {"e": "platform", "construct": "zos:if", "text": "(RC = 0"}),
])
def test_if_expressions_lower_or_stay_platform(text, want):
    assert source.if_expression(text) == want


# ---------------------------------------------------------------------- jobs, steps and procedures

SAMPLE = """//PAYD01   JOB (ACCT),'PROGRAMMER',CLASS=A,NOTIFY=&SYSUID
//         SET ENV=PROD,RUN=D1
//EXTRACT  PROC HLQ=&ENV,CYCLE=WEEKLY,OUT=SYSDA
//EXTR     EXEC PGM=PAYEXT
//PAYIN    DD DSN=&HLQ..PAY.MASTER,DISP=SHR
//PAYOUT   DD DSN=&HLQ..PAY.&CYCLE..EXTRACT,DISP=(NEW,CATLG),UNIT=&OUT
//LOG      DD SYSOUT=*
//CHECK    EXEC PGM=PAYCHK
//CHKIN    DD DSN=*.EXTR.PAYOUT,DISP=SHR
//         PEND
//CALL1    EXEC EXTRACT,CYCLE=DAILY
//EXTR.PAYIN DD DSN=TEST.PAY.MASTER,DISP=SHR
//EXTR.EXTRA DD DSN=PROD.PAY.EXTRA,DISP=MOD
//SORT1    EXEC PGM=SORT,COND=(4,LT)
//SORTIN   DD DSN=*.CALL1.EXTR.PAYOUT,DISP=SHR
//SORTOUT  DD DSN=PROD.PAY.SORTED.&UNSET,DISP=(NEW,CATLG)
//SYSIN    DD *
  SORT FIELDS=(1,10,CH,A,11,4,PD,D)
/*
//CALL2    EXEC MYPROC,REGION=0M
//STEPA.IN DD DSN=PROD.PAY.OTHER,DISP=SHR
"""

# Utilities, DD kinds and two jobs in one member. The PARM, NOTIFY, accounting and
# OUTPUT values are planted to show they are never copied.
SAMPLE_UTILITIES = """//PAYROLL1 JOB (12345,DEPT9),'J SMITH',CLASS=A,
//             MSGCLASS=X,NOTIFY=JSMITH01
/*JOBPARM SYSAFF=SYS1
//*%OPC SCAN
//OUT1     OUTPUT CLASS=A,DEST=RMT5
//         INCLUDE MEMBER=PAYSYMS
//         SET PROG=PAYCALC,RUN=D20240101
//STEP1    EXEC PGM=&PROG,PARM='SECRET=HUNTER2,USER=ADMIN',
//             REGION=0M
//INFILE   DD DSN=PAY.INPUT.&RUN,DISP=SHR
//         DD DSN=PAY.INPUT.EXTRA,DISP=SHR
//         DD DSN=PAY.LIB(MEMBER1),DISP=SHR
//CARDS    DD DATA,DLM=@@
//STEP9 DD ...
 MORE DATA
@@
//HFS      DD PATH='/u/pay/out.txt',PATHOPTS=(OWRONLY,OCREAT)
//NULLDD   DD DUMMY
//TEMPDD   DD DSN=&&WORK,DISP=(NEW,PASS)
//TEMP2    DD DSN=&TEMPX,DISP=(NEW,PASS)
//SYSDD    DD DSN=SYS1.MACLIB,DISP=SHR
//STEP2    EXEC PGM=*.STEP1.INFILE
//STEP3    EXEC PGM=ICETOOL
//TOOLMSG  DD SYSOUT=*
//IN1      DD DSN=PAY.MASTER,DISP=SHR
//OUT1     DD DSN=PAY.MASTER.UNIQUE,DISP=(NEW,CATLG)
//OUT2     DD DSN=PAY.MASTER.COPY,DISP=(NEW,CATLG)
//TOOLIN   DD *
  SELECT FROM(IN1) TO(OUT1) ON(1,8,CH) FIRST
  COPY FROM(IN1) TO(OUT2)
  SORT FROM(IN1) TO(OUT2) USING(CTL1)
/*
//CTL1CNTL DD *
  SORT FIELDS=(1,8,CH,D)
/*
//STEP4    EXEC PGM=SORT
//SORTJNF1 DD DSN=PAY.LEFT,DISP=SHR
//SORTJNF2 DD DSN=PAY.RIGHT,DISP=SHR
//SORTOUT  DD DSN=PAY.JOINED,DISP=(NEW,CATLG)
//GOOD     DD DSN=PAY.GOOD,DISP=(NEW,CATLG)
//SYSIN    DD *
  JOINKEYS FILE=F1,FIELDS=(1,8,A)
  JOINKEYS FILE=F2,FIELDS=(5,8,A)
  REFORMAT FIELDS=(F1:1,20,F2:13,10)
  SORT FIELDS=COPY
  OUTFIL FNAMES=GOOD,INCLUDE=(1,2,CH,EQ,C'OK')
/*
//STEP5    EXEC PGM=IDCAMS
//SYSIN    DD *
  DEFINE GDG (NAME(PAY.HISTORY) LIMIT(30) SCRATCH)
  DEFINE AIX (NAME(PAY.MASTER.AIX) RELATE(PAY.MASTER) -
      KEYS(8 20))
  DEFINE PATH (NAME(PAY.MASTER.PATH) PATHENTRY(PAY.MASTER.AIX))
  LISTCAT ENTRIES(PAY.*)
/*
//STEP6    EXEC PGM=IKJEFT1B
//SYSTSIN  DD *
  EX 'PAY.REXX(PAYRPT)'
  EXEC PAY.CLIST(PAYCLN)
  ALLOC FI(X) DA('PAY.X') SHR
/*
//STEP7    EXEC PGM=DFHCSDUP
//SYSIN    DD *
 DEFINE TRANSACTION(PAY1) GROUP(PAYGRP) PROGRAM(PAYINQ)
/*
//
//PAYROLL2 JOB ,CLASS=B
//S1       EXEC PGM=PAYPOST
//IN       DD DSN=*.PAYROLL1.STEP1.INFILE,DISP=SHR
"""


def test_jobs_tasks_and_member_alias():
    out = run(SAMPLE, "jcl/PAYDAILY.jcl")
    ents = entities(out)
    assert ents[job("PAYD01")]["kind"] == "job" and ents[job("PAYD01")]["member"] == "PAYDAILY"
    assert ents[job("PAYDAILY")]["platform_kind"] == "zos:Member"
    rels = relations(out)
    assert (job("PAYDAILY"), "ALIAS_OF", job("PAYD01")) in rels
    assert ents[job("EXTRACT")]["in_stream"] is True and (job("PAYD01"), "CONTAINS", job("EXTRACT")) in rels
    assert (job("EXTRACT"), "CONTAINS", task("EXTRACT", "EXTR")) in rels and (task("EXTRACT", "CHECK"), "DEPENDS_ON", task("EXTRACT", "EXTR")) in rels
    assert (task("PAYD01", "SORT1"), "DEPENDS_ON", task("PAYD01", "CALL1")) in rels
    assert (task("PAYD01", "CALL1"), "RUNS", job("EXTRACT")) in rels and (task("PAYD01", "CALL2"), "RUNS", job("MYPROC")) in rels
    assert (task("EXTRACT", "EXTR"), "RUNS", prog("PAYEXT")) in rels
    assert task("PAYD01", "EXTR") not in ents  # a procedure's steps belong to it
    assert ents[task("PAYD01", "SORT1")]["cond"] == "(4,LT)"
    assert ents[task("PAYD01", "CALL1")]["symbols"] == ["CYCLE"] and ents[task("PAYD01", "CALL1")]["overrides"] == ["EXTR.EXTRA", "EXTR.PAYIN"]


def test_no_alias_when_the_member_is_the_job_name():
    out = run("//PAYD01 JOB\n//S1 EXEC PGM=P1\n", "jcl/PAYD01.jcl")
    assert not any(p == "ALIAS_OF" for _, p, _ in relations(out))
    out = run("//S1 EXEC PGM=P1\n//I DD DSN=A.B,DISP=SHR\n// PEND\n", "proclib/NOHEAD.prc")
    assert job("NOHEAD") not in entities(out)  # no JOB or PROC statement: nothing is a job


def test_symbolics_resolve_by_precedence_and_overrides_apply():
    out = run(SAMPLE, "jcl/PAYDAILY.jcl")
    rels = qualified(out)
    call1 = task("PAYD01", "CALL1")
    got = {(p, o[1], q.get("dd"), q.get("step")) for s, p, o, q in rels if s == call1 and o[0] == "Dataset"}
    assert ("READS_FROM", json.dumps({"name": "test.pay.master"}), "PAYIN", "EXTR") in got  # the override replaces the procedure's DSN
    assert ("WRITES_TO", json.dumps({"name": "prod.pay.daily.extract"}), "PAYOUT", "EXTR") in got  # SET ENV -> HLQ=&ENV, CYCLE overridden
    assert ("WRITES_TO", json.dumps({"name": "prod.pay.extra"}), "EXTRA", "EXTR") in got  # an override that adds a DD
    assert ("READS_FROM", json.dumps({"name": "prod.pay.daily.extract"}), "CHKIN", "CHECK") in got  # referback inside the procedure
    ents = entities(out)
    assert ds("prod.pay.weekly.extract") not in ents and ds("prod.pay.master") not in ents
    # A referback to a procedure step from a job step.
    assert (task("PAYD01", "SORT1"), "READS_FROM", ds("prod.pay.daily.extract")) in relations(out)


def test_a_symbolic_with_no_value_is_kept_parameterized_and_counted():
    out = run(SAMPLE, "jcl/PAYDAILY.jcl")
    ents = entities(out)
    assert ents[ds("prod.pay.sorted.{unset}")]["parameterized"] is True
    assert any("&UNSET" in d.message for d in out.diagnostics)
    [summary] = of(out, "lineage_summary")
    assert summary["unresolved"] == 1 and summary["resolved"] >= 4
    assert (("zos:symbolic", False)) in constructs(out)


def test_a_cataloged_procedure_uses_its_defaults():
    text = "//MYPROC PROC HLQ=PROD,GEN=+1\n//STEPA EXEC PGM=PGMA\n//IN DD DSN=&HLQ..A.IN,DISP=SHR\n//OUT DD DSN=&HLQ..A.HIST(&GEN),DISP=(NEW,CATLG)\n"
    out = run(text, "proclib/MYPROC.prc")
    ents = entities(out)
    assert ents[job("MYPROC")]["kind"] == "proc" and ents[job("MYPROC")]["symbols"] == ["GEN", "HLQ"]
    assert ents[ds("prod.a.hist")]["gdg"] is True
    rels = relations(out)
    assert (task("MYPROC", "STEPA"), "READS_FROM", ds("prod.a.in")) in rels and (task("MYPROC", "STEPA"), "WRITES_TO", ds("prod.a.hist")) in rels


def test_a_call_of_a_cataloged_procedure_binds_only_its_own_dds():
    out = run(SAMPLE, "jcl/PAYDAILY.jcl")
    rels = qualified(out)
    assert (task("PAYD01", "CALL2"), "READS_FROM", ds("prod.pay.other"), {"dd": "IN"}) in rels
    assert not any(b["task"]["natural_key"]["name"] == "CALL2" for b in of(out, "dd_binding"))  # the program the step runs is in another file


def test_dd_kinds_temporary_system_library_dummy_sysout_and_concatenation():
    out = run(SAMPLE_UTILITIES, "jcl/PAYROLL.jcl")
    ents = entities(out)
    assert {ds("pay.input.d20240101"), ds("pay.input.extra"), ds("pay.lib")} <= set(ents)  # a concatenation, a member stripped
    assert not any(k[0] == "Dataset" and ("&" in k[1] or "sys1" in k[1]) for k in ents)
    c = constructs(out)
    for name in ("dd_concatenation", "dd_member", "dd_path", "dd_in_stream"):
        assert (f"zos:{name}", True) in c, name
    for name in ("dd_dummy", "dd_temp", "dd_system", "exec_dynamic", "exec_parm", "dd_sysout"):
        assert (f"zos:{name}", False) in c, name
    assert ents[task("PAYROLL1", "STEP2")]["program_dynamic"] is True
    assert not any(s == task("PAYROLL1", "STEP2") and p == "RUNS" for s, p, _ in relations(out))
    assert any("referback" in d.message for d in out.diagnostics)  # a referback to another job is not linked


def test_dd_bindings_name_task_program_dd_dataset_and_disp():
    out = run(SAMPLE, "jcl/PAYDAILY.jcl")
    rows = sorted((b["task"]["natural_key"]["name"], b["program"]["natural_key"]["name"], b["dd"], b["dataset"]["natural_key"]["name"], b["disp"], b["step"]) for b in of(out, "dd_binding"))
    assert ("CALL1", "PAYEXT", "PAYOUT", "prod.pay.daily.extract", "NEW", "CALL1.EXTR") in rows
    assert ("CALL1", "PAYCHK", "CHKIN", "prod.pay.daily.extract", "SHR", "CALL1.CHECK") in rows
    assert not any(r[1] == "SORT" for r in rows)  # a utility's DDs bind no program
    for b in of(out, "dd_binding"):
        assert set(b) == {"task", "program", "dd", "dataset", "disp", "job", "step"}
        assert b["program"]["declare"] is False and b["dataset"]["declare"] is False


def test_tso_steps_run_programs_execs_with_plans():
    out = run(SAMPLE_UTILITIES, "jcl/PAYROLL.jcl")
    rels = relations(out)
    assert (task("PAYROLL1", "STEP6"), "RUNS", prog("PAYRPT", "tso")) in rels and (task("PAYROLL1", "STEP6"), "RUNS", prog("PAYCLN", "tso")) in rels
    text = "//J1 JOB\n//S1 EXEC PGM=IKJEFT01\n//IN DD DSN=A.IN,DISP=SHR\n//SYSTSIN DD *\n DSN SYSTEM(DB2X)\n RUN PROGRAM(PGM1) PLAN(PLAN1) -\n   PARMS('/X')\n END\n %MYEXEC A B\n/*\n"
    out = run(text)
    ents, rels = entities(out), relations(out)
    assert ents[task("J1", "S1")]["plan"] == "PLAN1" and ents[task("J1", "S1")]["execs"] == ["MYEXEC"]
    assert (task("J1", "S1"), "RUNS", prog("PGM1")) in rels and (task("J1", "S1"), "RUNS", prog("MYEXEC", "tso")) in rels
    assert (task("J1", "S1"), "DEPENDS_ON", prog("PLAN1", "db2-plan")) in rels
    [b] = of(out, "dd_binding")
    assert b["program"]["natural_key"]["name"] == "PGM1" and b["dd"] == "IN"
    assert "DB2X" not in blob_of(out) and "/X" not in blob_of(out)


def test_idcams_definitions_deletes_and_repro():
    out = run(F38.joinpath("jcl/ACCTDLY.jcl").read_text(), "jcl/ACCTDLY.jcl")
    ents = entities(out)
    master = ents[ds("prod.acct.master")]
    assert (master["vsam"], master["key_length"], master["key_offset"], master["record_length"], master["average_record_length"]) == ("ksds", 10, 0, 200, 200)
    assert ds("prod.acct.master.data") not in ents
    q = qualified(out)
    reload = task("ACCTD01", "RELOAD")
    assert (reload, "WRITES_TO", ds("prod.acct.master"), {"op": "delete"}) in q and (reload, "READS_FROM", ds("prod.acct.master.backup"), {"op": "repro"}) in q
    out = run(SAMPLE_UTILITIES, "jcl/PAYROLL.jcl")
    ents = entities(out)
    assert ents[ds("pay.history")]["gdg_limit"] == 30 and ents[ds("pay.master.aix")]["alternate_index_of"] == "pay.master"
    assert (ds("pay.master.path"), "ALIAS_OF", ds("pay.master.aix")) in relations(out)


# ---------------------------------------------------------------------- transform shapes


def test_dfsort_shapes_in_order_and_valid():
    out = run(F38.joinpath("jcl/ACCTDLY.jcl").read_text(), "jcl/ACCTDLY.jcl")
    [unit] = of(out, "transform")
    assert transforms.validate_shape(unit) == [] and unit["language"] == "zos"
    ops = [t["op"] for t in unit["transforms"]]
    assert ops == ["filter", "project", "sort", "aggregate"]
    t = unit["transforms"]
    assert t[0]["inputs"] == ["prod.acct.daily.extract"] and t[0]["predicate"]["op"] == "and"
    assert t[1]["columns"] == {"rename": {"50:8": "11:8"}} and t[1]["primitive"] == "select_columns"
    assert t[2]["order"] == [{"column": "1:10", "desc": False}]
    assert t[3]["measures"] == [{"fn": "sum", "column": "11:8", "as": "11:8"}] and t[3]["keys"] == ["1:10"]
    assert t[3]["output"] == "prod.acct.summary.{rundate}" and t[3]["dynamic"] is True


def test_icetool_joinkeys_outfil_and_omit():
    out = run(SAMPLE_UTILITIES, "jcl/PAYROLL.jcl")
    units = {u["owner"]["natural_key"]["name"]: u for u in of(out, "transform")}
    for u in units.values():
        assert transforms.validate_shape(u) == [], transforms.validate_shape(u)
    ice = units["STEP3"]["transforms"]
    assert [t["op"] for t in ice] == ["deduplicate", "read", "sort"] and ice[0]["primitive"] == "dedupe_by_key" and ice[0]["output"] == "pay.master.unique"
    join = units["STEP4"]["transforms"]
    assert join[0]["op"] == "join" and join[0]["inputs"] == ["pay.left", "pay.right"] and join[0]["join"] == {"type": "inner", "on": [["1:8", "5:8"]]}
    assert join[1]["op"] == "filter" and join[1]["output"] == "pay.good"
    text = "//J1 JOB\n//S1 EXEC PGM=SORT\n//SORTIN DD DSN=A.IN,DISP=SHR\n//SORTOUT DD DSN=A.OUT,DISP=(NEW,CATLG)\n//SYSIN DD *\n  OMIT COND=(5,1,CH,EQ,C'D')\n  SORT FIELDS=(1,4,ZD,A)\n  SUM FIELDS=NONE\n  OUTREC FIELDS=(1,4,C'X',10:5,2)\n/*\n"
    [u] = of(run(text), "transform")
    assert transforms.validate_shape(u) == []
    assert [t["op"] for t in u["transforms"]] == ["filter", "sort", "deduplicate", "project"]
    assert u["transforms"][0]["predicate"]["e"] == "unop" and u["transforms"][2]["primitive"] == "dedupe_by_key"
    assert u["transforms"][3]["output"] == "a.out"


def test_an_unreadable_sort_statement_is_a_platform_node_not_a_guess():
    text = "//J1 JOB\n//S1 EXEC PGM=SORT\n//SORTIN DD DSN=A.IN,DISP=SHR\n//SYSIN DD *\n  INCLUDE COND=(SS,EQ,C'X')\n  SORT FIELDS=(NAME,A)\n/*\n"
    [u] = of(run(text), "transform")
    assert transforms.validate_shape(u) == []
    assert u["transforms"][0]["predicate"]["e"] == "platform"
    assert u["transforms"][1] ["op"] == "platform"


def test_a_control_member_is_owned_by_the_file_with_placeholder_inputs():
    out = run(F38.joinpath(".rkp/inputs/zos/cntl/ACCTDUP.ctl").read_text(), ".rkp/inputs/zos/cntl/ACCTDUP.ctl")
    [u] = of(out, "transform")
    assert u["owner"] == "@file" and transforms.validate_shape(u) == []
    assert u["transforms"][0]["inputs"] == ["{sortin}"] and u["transforms"][-1]["output"] == "{sortout}" and u["transforms"][-1]["dynamic"] is True


# ---------------------------------------------------------------------- behaviour


def test_flow_units_are_valid_with_cond_and_if():
    out = run(F38.joinpath("jcl/ACCTDLY.jcl").read_text(), "jcl/ACCTDLY.jcl")
    units = {u["owner"]["natural_key"]["name"]: u for u in of(out, "behavior")}
    assert set(units) == {"ACCTD01", "EXTRACT"}
    for u in units.values():
        assert behavior.validate_unit(u) == [], behavior.validate_unit(u)
        assert u["kind"] == "flow" and u["language"] == "zos"
    body = units["ACCTD01"]["body"]
    assert [n["op"] for n in body] == ["call", "call", "platform", "call", "if"]
    assert body[2]["construct"] == "zos:cond" and body[2]["body"][0]["call"]["callee"]["name"] == "SORTSUM"
    assert body[1]["call"]["named"] == {"procedure": {"e": "lit", "value": "EXTRACT", "type": "string"}}
    assert body[4]["cond"] == {"e": "binop", "op": "==", "left": {"e": "member", "object": {"e": "var", "scope": "step", "name": "POST"}, "name": "RC"}, "right": {"e": "lit", "value": 0, "type": "number"}}
    assert body[4]["else"][0]["call"]["callee"]["name"] == "ALERT"


# ---------------------------------------------------------------------- definitions

CSD = (F38 / ".rkp/inputs/zos/csd/ACCTGRP.csd").read_text()
DCLGEN = (F38 / ".rkp/inputs/zos/dclgen/DACCT.dclgen").read_text()
BIND = (F38 / ".rkp/inputs/zos/bind/ACCTBIND.bind").read_text()


def test_definitions_match_inputs_and_suffixes_but_not_sql_or_jcl():
    d = ZosDefinitions()
    assert d.matches(".rkp/inputs/zos/anything.txt") and d.matches(".RKP/INPUTS/ZOS/X") and d.matches("defs/ACCT.csd") and d.matches("ctl/SORT1.ctl")
    assert not d.matches(".rkp/inputs/zos/ddl/x.sql") and not d.matches(".rkp/inputs/zos/x.jcl") and not d.matches("src/a.cbl")
    out = run("hello\n", ".rkp/inputs/zos/notes.txt")
    assert out.parsed is False and out.observations == []


def test_csd_transactions_are_endpoints_their_program_exposes():
    out = run(CSD, ".rkp/inputs/zos/csd/ACCTGRP.csd")
    ents, rels = entities(out), relations(out)
    ep = ents[endpoint("ACIN")]
    assert (ep["program"], ep["program_language"], ep["plan"], ep["group"]) == ("ACCTINQ", "COBOL", "ACCTPLN", "ACCTGRP")
    assert (prog("ACCTINQ"), "EXPOSES", endpoint("ACIN")) in rels
    assert ents[ds("acctmst")]["access"] == "cics_file" and ents[ds("acctmst")]["operations"] == ["browse", "read"]
    assert (ds("acctmst"), "ALIAS_OF", ds("prod.acct.master")) in rels
    assert "ACCOUNT INQUIRY" not in blob_of(out)  # descriptions are never copied
    [x] = [o for o in out.observations if o.kind == "relation" and o.payload["predicate"] == "EXPOSES"]
    assert x.payload["subject"]["declare"] is False


def test_csd_in_a_dfhcsdup_step_and_a_twice_defined_file():
    out = run(SAMPLE_UTILITIES, "jcl/PAYROLL.jcl")
    assert (prog("PAYINQ"), "EXPOSES", endpoint("PAY1")) in relations(out)
    text = "DEFINE FILE(F1) GROUP(G) DSNAME(A.ONE)\nDEFINE FILE(F1) GROUP(H) DSNAME(A.TWO)\n"
    out = run(text, ".rkp/inputs/zos/f.csd")
    assert (ds("f1"), "ALIAS_OF", ds("a.one")) in relations(out) and (ds("f1"), "ALIAS_OF", ds("a.two")) not in relations(out)
    assert any(d.severity == "warning" for d in out.diagnostics)


def test_dclgen_schema_fields_and_columns():
    out = run(DCLGEN, ".rkp/inputs/zos/dclgen/DACCT.dclgen")
    ents = entities(out)
    schema = ents[("DataSchema", json.dumps({"kind": "dclgen", "name": "DACCT"}, sort_keys=True))]
    assert (schema["table"], schema["record"], schema["columns"], schema["fields"]) == ("acct.account", "DCLACCOUNT", 4, 6)

    def field(name):
        return ents[("Field", json.dumps({"name": name, "table": "zos:DACCT"}, sort_keys=True))]

    assert field("BALANCE")["usage"] == "COMP-3" and field("BALANCE")["length"] == 6 and field("BALANCE")["offset"] == 52 and field("BALANCE")["nullable"] is True
    assert field("ACCT-NAME")["group"] is True and field("ACCT-NAME")["sql_type"] == "VARCHAR(40)" and field("ACCT-NAME-TEXT")["offset"] == 12
    assert not any(k[0] == "Table" for k in ents)  # the table belongs to the DDL and the SQL that name it


def test_bind_packages_and_plans_never_copy_owner_or_qualifier():
    out = run(BIND, ".rkp/inputs/zos/bind/ACCTBIND.bind")
    ents, rels = entities(out), relations(out)
    assert ents[prog("ACCTCOLL.ACCTPOST", "db2-package")]["member"] == "ACCTPOST"
    assert ents[prog("ACCTPLN", "db2-plan")]["collections"] == ["ACCTCOLL"]
    assert (prog("ACCTCOLL.ACCTPOST", "db2-package"), "DEPENDS_ON", prog("ACCTPOST")) in rels
    text = "BIND PLAN(PLANX) PKLIST(LOC1.COLLA.PKG1, COLLB.*) MEMBER(OLDPGM) OWNER(SECRETID)\n"
    out = run(text, ".rkp/inputs/zos/x.bind")
    ents, rels = entities(out), relations(out)
    assert ents[prog("PLANX", "db2-plan")]["packages"] == ["COLLA.PKG1"] and ents[prog("PLANX", "db2-plan")]["collections"] == ["COLLB"]
    assert (prog("PLANX", "db2-plan"), "CONTAINS", prog("COLLA.PKG1", "db2-package")) in rels and (prog("PLANX", "db2-plan"), "DEPENDS_ON", prog("OLDPGM")) in rels
    for o in (run(BIND, ".rkp/inputs/zos/b.bind"), out):
        assert "ACCTOWN" not in blob_of(o) and "ACCTQUAL" not in blob_of(o) and "SECRETID" not in blob_of(o) and "DB2P" not in blob_of(o)


def test_db2_ddl_outside_sql_is_read_as_sql_ddl_reads_it():
    from repo_intel.extractors.contracts_data import SqlDdl

    text = (F38 / ".rkp/inputs/zos/ddl/ACCOUNT.ddl").read_text()
    out = run(text, ".rkp/inputs/zos/ddl/ACCOUNT.ddl")
    ents = entities(out)
    plain = "CREATE TABLE ACCT.ACCOUNT\n  ( ACCT_ID CHAR(10) NOT NULL,\n    ACCT_NAME VARCHAR(40) NOT NULL,\n    BALANCE DECIMAL(11,2) NOT NULL,\n    OPENED DATE NOT NULL,\n    PRIMARY KEY (ACCT_ID)\n  );\n"
    want = entities(SqlDdl().extract("schema.sql", plain.encode(), "b"))
    assert set(want) <= set(ents)
    assert not any("ACCTTS" in k[1] or "XACCT01" in k[1] for k in ents)
    c = constructs(out)
    assert c[("zos:ddl_table", True)] == 1 and c[("zos:ddl_storage", False)] == 1 and c[("zos:ddl_index", False)] == 1


# ---------------------------------------------------------------------- names, never values; determinism; speed



def test_values_are_never_copied():
    for out in (run(SAMPLE_UTILITIES, "jcl/PAYROLL.jcl"), run(SAMPLE, "jcl/PAYDAILY.jcl")):
        text = blob_of(out)
        for secret in ("HUNTER2", "ADMIN", "JSMITH01", "J SMITH", "PROGRAMMER", "12345", "DEPT9", "RMT5", "SYSAFF", "0M"):
            assert secret not in text, secret


def test_output_is_deterministic():
    for text, path in ((SAMPLE, "jcl/PAYDAILY.jcl"), (SAMPLE_UTILITIES, "jcl/PAYROLL.jcl"), (CSD, ".rkp/inputs/zos/a.csd"), (DCLGEN, ".rkp/inputs/zos/D.dclgen")):
        a, b = run(text, path), run(text, path)
        assert [(o.kind, o.payload) for o in a.observations] == [(o.kind, o.payload) for o in b.observations]


def test_large_and_hostile_inputs_stay_linear():
    big = "//BIG JOB\n" + "".join(f"//S{i:05d} EXEC PGM=P{i % 97:04d}\n//IN DD DSN=A.B{i % 50}.C,DISP=SHR\n//         DD DSN=A.D,\n//            DISP=SHR\n" for i in range(4000))
    t = time.perf_counter()
    out = run(big, "jcl/BIG.jcl")
    assert time.perf_counter() - t < 15
    assert len(of(out, "dd_binding")) == 8000
    hostile = "//H JOB\n//S1 EXEC PGM=P1,PARM='" + "(" * 5000 + "\n" + "//X DD DSN=" + "A." * 3000 + "\n" + "//  IF " + "(" * 3000 + "RC = 0 THEN\n" + "//S2 EXEC PGM=SORT\n//SYSIN DD *\n INCLUDE COND=(" + "(1,2,CH,EQ,C'A'),OR," * 2000 + ")\n/*\n"
    t = time.perf_counter()
    run(hostile, "jcl/H.jcl")
    run("DEFINE FILE(" + "(" * 5000 + "\n" + "BIND PACKAGE(" * 2000 + "\n", ".rkp/inputs/zos/h.csd")
    run("       01 A.\n" + "           05 B PIC X(10).\n" * 3000, ".rkp/inputs/zos/h.dclgen")
    assert time.perf_counter() - t < 15


# ---------------------------------------------------------------------- the linker pass


def _summary(path, *observations):
    return {"path": path, "observations": [{"kind": k, "payload": p, "evidence": {"path": path, "n": i}} for i, (k, p) in enumerate(observations)]}


def _program(name):
    return {"kind": "Program", "natural_key": {"platform": "mainframe", "name": name}, "name": name}


def _binding(program, dd, dataset, disp, job_name="J1", step="S1"):
    return ("dd_binding", {"task": {"kind": "Task", "natural_key": {"framework": "jcl", "job": job_name, "name": step}, "name": step}, "program": {**_program(program), "declare": False}, "dd": dd,
                           "dataset": {"kind": "Dataset", "natural_key": {"name": dataset}, "name": dataset, "declare": False}, "disp": disp, "job": job_name, "step": step})


def _dd_file(program, dd, mode):
    return ("dd_file", {"program": _program(program), "dd": dd, "mode": mode, "select": "F-" + dd, "record": "R-" + dd})


def test_link_dd_datasets_joins_programs_to_the_data_sets_their_jcl_binds():
    from repo_intel.compile.link import _Linker

    summaries = [
        _summary("cobol/PGM1.cbl", _dd_file("PGM1", "INFILE", "input"), _dd_file("PGM1", "OUTFILE", "output"), _dd_file("PGM1", "MASTER", "i-o"), _dd_file("PGM1", "LOG", None), _dd_file("PGM1", "UNBOUND", "input")),
        _summary("cobol/PGM2.cbl", _dd_file("PGM2", "INFILE", "extend")),
        _summary("jcl/J1.jcl", _binding("PGM1", "INFILE", "a.in", "SHR"), _binding("PGM1", "OUTFILE", "a.out", "NEW"), _binding("PGM1", "MASTER", "a.master", "OLD"),
                 _binding("PGM1", "LOG", "a.log", "MOD"), _binding("PGM1", "INFILE", "b.in", "SHR", "J2", "S9"), _binding("PGM3", "INFILE", "c.in", "SHR")),
        _summary("jcl/J2.jcl", _binding("PGM2", "infile", "a.out", "SHR")),
    ]
    linker = _Linker({"name": "@root"}, summaries, [])
    linker.link_dd_datasets()
    got = sorted((r["path"], r["payload"]["subject"]["natural_key"]["name"], r["payload"]["predicate"], r["payload"]["object"]["natural_key"]["name"], json.dumps(r["payload"]["qualifiers"], sort_keys=True))
                 for r in linker.out if r["kind"] == "relation")
    assert got == [
        ("cobol/PGM1.cbl", "PGM1", "READS_FROM", "a.in", json.dumps({"dd": "INFILE", "via": {"job": "J1", "step": "S1"}}, sort_keys=True)),
        ("cobol/PGM1.cbl", "PGM1", "READS_FROM", "a.master", json.dumps({"dd": "MASTER", "via": {"job": "J1", "step": "S1"}}, sort_keys=True)),
        ("cobol/PGM1.cbl", "PGM1", "READS_FROM", "b.in", json.dumps({"dd": "INFILE", "via": {"job": "J2", "step": "S9"}}, sort_keys=True)),
        ("cobol/PGM1.cbl", "PGM1", "WRITES_TO", "a.log", json.dumps({"dd": "LOG", "via": {"job": "J1", "step": "S1"}}, sort_keys=True)),
        ("cobol/PGM1.cbl", "PGM1", "WRITES_TO", "a.master", json.dumps({"dd": "MASTER", "via": {"job": "J1", "step": "S1"}}, sort_keys=True)),
        ("cobol/PGM1.cbl", "PGM1", "WRITES_TO", "a.out", json.dumps({"dd": "OUTFILE", "via": {"job": "J1", "step": "S1"}}, sort_keys=True)),
        ("cobol/PGM2.cbl", "PGM2", "WRITES_TO", "a.out", json.dumps({"dd": "INFILE", "via": {"job": "J1", "step": "S1"}}, sort_keys=True)),
    ]
    for r in linker.out:
        assert r["evidence_class"] == "E3" and len(r["evidence"]) == 2
        assert r["payload"]["subject"]["declare"] is False and r["payload"]["object"]["declare"] is False
    # Order of the summaries does not change the result.
    again = _Linker({"name": "@root"}, list(reversed(summaries)), [])
    again.link_dd_datasets()
    assert [(r["path"], r["payload"]) for r in again.out] == [(r["path"], r["payload"]) for r in linker.out]


def test_link_dd_datasets_with_no_bindings_or_no_files_adds_nothing():
    from repo_intel.compile.link import _Linker

    for summaries in ([_summary("cobol/P.cbl", _dd_file("P", "IN", "input"))], [_summary("jcl/J.jcl", _binding("P", "IN", "a.in", "SHR"))], []):
        linker = _Linker({"name": "@root"}, summaries, [])
        linker.link_dd_datasets()
        assert linker.out == []


def test_dd_bindings_from_jcl_join_dd_files_end_to_end():
    from repo_intel.compile.link import _Linker

    data = (F38 / "jcl/ACCTDLY.jcl").read_bytes()
    out = ZosJcl().extract("jcl/ACCTDLY.jcl", data, "b")
    rows = [{"kind": o.kind, "payload": json.loads(json.dumps(o.payload, default=str)), "evidence": {"path": "jcl/ACCTDLY.jcl"}} for o in out.observations if o.kind == "dd_binding"]
    summaries = [{"path": "jcl/ACCTDLY.jcl", "observations": rows}, _summary("cobol/ACCTEXT.cbl", _dd_file("ACCTEXT", "ACCTIN", "input"), _dd_file("ACCTEXT", "ACCTOUT", "output")),
                 _summary("cobol/ACCTPOST.cbl", _dd_file("ACCTPOST", "ACCTIN", "input"))]
    linker = _Linker({"name": "@root"}, summaries, [])
    linker.link_dd_datasets()
    got = {(r["payload"]["subject"]["natural_key"]["name"], r["payload"]["predicate"], r["payload"]["object"]["natural_key"]["name"], r["payload"]["qualifiers"]["via"]["step"]) for r in linker.out}
    assert got == {("ACCTEXT", "READS_FROM", "prod.acct.master", "EXTRACT1.EXTR"), ("ACCTEXT", "WRITES_TO", "prod.acct.daily.extract", "EXTRACT1.EXTR"),
                   ("ACCTPOST", "READS_FROM", "prod.acct.daily.extract", "POST")}
