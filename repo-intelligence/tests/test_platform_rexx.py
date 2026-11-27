"""The REXX and CLIST pack (repo_intel/platforms/rexx/): execs as Program {platform: tso},
the programs and execs they run, the datasets they allocate and read or write, from one
file's bytes; commands built at run time stay dynamic and are counted."""

import hashlib
from pathlib import Path

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.platforms import packs, validate_pack
from repo_intel.platforms.rexx import PACK
from repo_intel.platforms.rexx.source import strip_comments
from repo_intel.platforms.rexx.tso import DYN, Command, operands
from test_platform_pli import check_all, constructs, doubling_ratio, entities, key, of, relations, units

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "F41-pli-easytrieve"
RUNRPT = key("Program", platform="tso", name="RUNRPT")


def run(path: str, data: bytes | str):
    raw = data.encode() if isinstance(data, str) else data
    x = next(e for e in PACK.extractors if e.matches(path))
    return x.extract(path, raw, hashlib.sha256(raw).hexdigest())


def mainframe(name: str) -> tuple:
    return key("Program", platform="mainframe", name=name)


def test_the_pack_is_registered_and_valid():
    assert packs()["rexx"] is PACK and validate_pack(PACK) == []
    x = PACK.extractors[0]
    assert x.id == "rexx.source"
    for path in ("a/RUN.rexx", "a/RUN.REX", "a/NIGHT.clist", "prod/exec/RUNRPT", "SYS1/SYSPROC/NIGHTLY"):
        assert x.matches(path), path
    for path in ("a/README", "a/run.sh", "exec/notes.txt"):
        assert not x.matches(path), path


def test_tso_command_operands():
    ops = operands("ALLOC FI(IN) DA('PROD.A(B)' 'X.Y') SHR REUSE")
    assert [(o.word, o.value) for o in ops] == [("ALLOC", None), ("FI", "IN"), ("DA", "'PROD.A(B)' 'X.Y'"), ("SHR", None), ("REUSE", None)]
    cmd = Command.read("CALL 'PROD.LOAD(CUSTUPD)' 'parm'")
    assert cmd.verb == "CALL" and cmd.operands[0].value == "PROD.LOAD(CUSTUPD)"
    assert strip_comments("a /* b /* c */ d */ e '/*' f") == "a" + " " * 19 + "e '/*' f"


def test_the_fixture_exec():
    out = run("rexx/RUNRPT.rexx", (FIXTURE / "base/rexx/RUNRPT.rexx").read_bytes())
    check_all(out)
    assert units(out) == []
    ents, rels = entities(out), relations(out)
    prog = ents[RUNRPT]
    assert prog["platform_kind"] == "rexx:Exec" and prog["language"] == "rexx" and prog["environments"] == ["ISPEXEC", "LINKMVS", "TSO"]
    assert prog["allocations"] == [
        {"dd": "CUSTIN", "datasets": ["prod.cust.master"], "disp": "SHR"},
        {"dd": "CUSTNEW", "datasets": ["prod.cust.master.new"], "disp": "MOD"},
        {"dd": "REGLIST", "datasets": [], "disp": "SHR", "dynamic": True},
    ]
    assert {f["dd"]: f["mode"] for f in prog["files"]} == {"AUDITDD": "output", "CUSTIN": "input", "CUSTNEW": "output", "REGLIST": "input"}
    for name in ("CUSTUPD", "CUSTRPT", "BALSUM"):  # LINKMVS, ISPEXEC SELECT PGM, a queued DSN RUN PROGRAM
        assert rels[(RUNRPT, "CALLS", mainframe(name))]["object"]["declare"] is False
    assert (RUNRPT, "CALLS", key("Program", platform="tso", name="NOTIFY")) in rels
    assert (RUNRPT, "READS_FROM", key("Dataset", name="prod.cust.master")) in rels
    assert (RUNRPT, "WRITES_TO", key("Dataset", name="prod.cust.master.new")) in rels
    assert ents[key("Dataset", name="prod.cust.master")]["platform"] == "mainframe"
    # LOGRUN is internal; the interpreted call and the prefixed allocation stay dynamic.
    assert not any("LOGRUN" in r[2][1] for r in rels)
    assert of(out, "lineage_summary")[0] == {"resolved": 2, "unresolved": 3}
    dd = of(out, "dd_file")
    assert [(d["dd"], d["mode"]) for d in dd] == [("AUDITDD", "output")]  # not allocated here: bound by the caller
    counts = constructs(out)
    assert counts[("rexx:interpret", False)] == 1 and counts[("rexx:alloc", False)] == 1 and counts[("rexx:link_program", True)] == 1


def test_no_value_is_copied():
    out = run("rexx/RUNRPT.rexx", (FIXTURE / "base/rexx/RUNRPT.rexx").read_bytes())
    text = str([o.payload for o in out.observations]) + str([d.message for d in out.diagnostics])
    for value in ("fixture-not-a-real-password-0041", "BALPLAN", "DB2P", "SUMMARY"):
        assert value not in text, value


EXEC = """/* rexx */
address tso
dsn = 'PROD.ORDERS'
"ALLOC F(IN) DA('"dsn"') SHR"
"CALL 'PROD.LOADLIB(ORDLOAD)'"
"CALL *(ORDCHK) 'X'"
"EX 'PROD.EXEC(SUBEXEC)'"
pgm = 'ORDX'
address linkmvs pgm
address attchmvs 'ORDSUB P1'
address ispexec
"SELECT CMD(%ORDCMD 1)"
"SELECT PGM("pgm")"
address dsnrexx "EXECSQL SELECT ID INTO :ID FROM ORDERS WHERE X = 1"
address dsnrexx "EXECSQL PREPARE S1 FROM :STMT"
address tso "SUBMIT 'PROD.JCL(ORDJOB)'"
x = ORDFUNC(1) + length('a')
call ORDRTN 1
address value newenv
"anything"
"""


def test_commands_programs_and_dynamic_parts():
    out = run("exec/ORDERS", EXEC)
    check_all(out)
    ents, rels = entities(out), relations(out)
    me = key("Program", platform="tso", name="ORDERS")
    assert ents[me]["submits"] == ["PROD.JCL(ORDJOB)"]
    for name in ("ORDLOAD", "ORDCHK", "ORDSUB"):
        assert (me, "CALLS", mainframe(name)) in rels, name
    for name in ("SUBEXEC", "ORDCMD", "ORDFUNC", "ORDRTN"):
        assert (me, "CALLS", key("Program", platform="tso", name=name)) in rels, name
    assert (me, "READS_FROM", key("Table", datastore="default", name="orders")) in rels
    # pgm is assigned, so LINKMVS pgm and SELECT PGM("pgm") are dynamic; so are the
    # allocation, the PREPAREd SQL and the commands after ADDRESS VALUE.
    assert mainframe("PGM") not in {r[2] for r in rels} and mainframe("ORDX") not in {r[2] for r in rels}
    assert not any(k[0] == "Dataset" for k in ents)
    counts = constructs(out)
    assert counts[("rexx:link_program", False)] == 1 and counts[("rexx:sql_dynamic", False)] == 1
    assert counts[("rexx:address_dynamic", False)] == 1 and counts[("rexx:command", False)] == 1
    assert of(out, "lineage_summary")[0]["unresolved"] >= 5


CLIST = """PROC 1 RUNDATE
/* NIGHTLY: allocate the run log, update the customers, then report
CONTROL NOMSG
ALLOC FI(RUNLOG) DA('PROD.CUST.RUNLOG') MOD REUSE
ALLOC FI(PARMS) DA(&RUNDATE..PARMS) SHR
OPENFILE RUNLOG OUTPUT
OPENFILE PARMS
SET &RUNLOG = &STR(START &RUNDATE)
PUTFILE RUNLOG
CALL 'PROD.LOADLIB(CUSTUPD)'
IF &LASTCC NE 0 THEN +
   GOTO FAILED
%RUNRPT &RUNDATE
CALL '&LIB(&PGM)'
DATA
  RUN PROGRAM(NOTREAD)
ENDDATA
EXIT CODE(0)
FAILED: WRITE FAILED WITH &LASTCC
"""


def test_a_clist():
    out = run("clist/NIGHTLY.clist", CLIST)
    check_all(out)
    ents, rels = entities(out), relations(out)
    me = key("Program", platform="tso", name="NIGHTLY")
    assert ents[me]["language"] == "clist"
    assert ents[me]["files"] == [{"dd": "PARMS", "mode": "input"}, {"dd": "RUNLOG", "mode": "output"}]
    assert (me, "WRITES_TO", key("Dataset", name="prod.cust.runlog")) in rels
    assert (me, "CALLS", mainframe("CUSTUPD")) in rels and (me, "CALLS", key("Program", platform="tso", name="RUNRPT")) in rels
    assert mainframe("NOTREAD") not in {r[2] for r in rels}  # lines between DATA and ENDDATA are data
    counts = constructs(out)
    assert counts[("rexx:clist_file", True)] == 1 and counts[("rexx:clist_file", False)] == 1 and counts[("rexx:tso_call", False)] == 1
    assert "PARMS" in " ".join(d.message for d in out.diagnostics)


def test_a_member_without_suffix_is_rexx_by_its_first_comment():
    rexx = run("lib/clist/DRIVER", "\n  \n/* REXX */\n\"CALL 'A.B(PGM1)'\"\n")  # the first non-blank line
    clist = run("lib/clist/DRIVER", "PROC 0\nCALL 'A.B(PGM1)'\n")
    assert entities(rexx)[key("Program", platform="tso", name="DRIVER")]["language"] == "rexx"
    assert entities(clist)[key("Program", platform="tso", name="DRIVER")]["language"] == "clist"
    assert DYN not in str([o.payload for o in rexx.observations])
    assert entities(run("prod/exec/RUNRPT", "/* rexx driver */\nADDRESS LINKMVS 'CUSTUPD'\n"))[key("Program", platform="tso", name="RUNRPT")]["language"] == "rexx"


def test_extraction_is_linear_with_many_notes():
    # Each note (an INTERPRET, an allocation built at run time) counted lines from the start
    # of the file.
    def rexx(n: int) -> str:
        return "/* REXX */\n" + "".join(f"interpret 'say' x{i}\n\"ALLOC FI(D{i % 90}) DA('\"p\".X') SHR\"\n" for i in range(n))

    def clist(n: int) -> str:
        return "PROC 0\n" + "".join(f"OPENFILE &D{i}\nSET &A{i} = 1 /* a */ /* b */\n" for i in range(n))

    out = run("rexx/BIG.rexx", rexx(50))
    assert sum("INTERPRET" in d.message for d in out.diagnostics) == 50
    assert doubling_ratio(lambda t: run("rexx/BIG.rexx", t), rexx, 2000) < 3
    assert sum("OPENFILE" in d.message for d in run("prod/clist/BIG", clist(50)).diagnostics) == 50
    assert doubling_ratio(lambda t: run("prod/clist/BIG", t), clist, 2000) < 3


def test_a_member_without_suffix_and_without_evidence_gives_nothing():
    script = "#!/bin/sh\nset -e\nexit 0\n"
    x = PACK.extractors[0]
    assert x.matches("docker/exec/entrypoint")  # the path alone cannot tell
    for path, text in (
        ("docker/exec/entrypoint", script),  # a shell script in an exec directory
        ("tools/clist/entrypoint", script),  # starts with #!, even in a CLIST directory
        ("tools/clist/ENTRY", "#!/bin/sh\nPROC 0\nEXIT CODE(0)\n"),
        ("prod/exec/NOTES", "Run the nightly batch first.\nThen the report.\n"),  # no REXX comment
        ("prod/sysproc/README", "set -e\nexit 0\nwrite the docs\n"),  # no CLIST statement: lower-case shell words
        ("prod/clist/CALLONLY", "CALL 'A.B(PGM1)'\n"),  # a TSO command alone is not a CLIST statement
    ):
        out = run(path, text)
        assert out.observations == [] and out.diagnostics == [] and out.parsed is False, path
    # One CLIST statement is enough in a CLIST directory; a comment holding one is not.
    for body in ("CONTROL NOMSG", "SET &A = 1", "&A = 1", "WRITE HELLO", "ALLOC FI(X) DA('A.B') SHR", "EXIT", "EXIT CODE(4)", "L1: GOTO L1"):
        out = run("prod/clist/ONE", body + "\n")
        assert entities(out)[key("Program", platform="tso", name="ONE")]["language"] == "clist", body
    assert run("prod/clist/ONE", "/* CONTROL NOMSG */\nCALL 'A.B(P)'\n").observations == []
    assert run("prod/exec/ONE", "CONTROL NOMSG\n").observations == []  # not a CLIST directory
