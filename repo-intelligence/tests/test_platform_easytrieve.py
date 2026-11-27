"""The Easytrieve pack (repo_intel/platforms/easytrieve/): FILE layouts as DataSchemas and
Fields, files by DD name, JOB activities and procedures as behaviour units, SORT as a
transform shape, REPORT sections as Reports, CALL and SQL, from one file's bytes."""

import hashlib
from pathlib import Path

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import walk
from repo_intel.compile.transforms import validate_shape
from repo_intel.platforms import packs, validate_pack
from repo_intel.platforms.easytrieve import PACK
from repo_intel.platforms.easytrieve.reader import statements
from test_platform_pli import check_all, constructs, entities, key, of, relations, units

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "F41-pli-easytrieve"
PROGRAM = key("Program", platform="mainframe", name="CUSTRPT")


def run(path: str, data: bytes | str):
    raw = data.encode() if isinstance(data, str) else data
    x = next(e for e in PACK.extractors if e.matches(path))
    return x.extract(path, raw, hashlib.sha256(raw).hexdigest())


def fixture(commit: str = "base"):
    return run("ezt/CUSTRPT.ezt", (FIXTURE / commit / "ezt/CUSTRPT.ezt").read_bytes())


def test_the_pack_is_registered_and_valid():
    assert packs()["easytrieve"] is PACK and PACK.prefix == "ezt"
    assert validate_pack(PACK) == []
    assert [x.id for x in PACK.extractors] == ["easytrieve.source"]
    assert PACK.extractors[0].matches("lib/CUSTRPT.EZT") and not PACK.extractors[0].matches("lib/CUSTRPT.txt")


def test_reader_statements_labels_and_continuations():
    text = "* comment\nFILE IN1 FB(80 800)\nCHECK. PROC\n  SQL INSERT INTO T +\n      VALUES (1)\nEND-PROC\nA = 1. B = 2\n" + "C = 3".ljust(72) + "SEQ00001\n"
    got = [(s.label, [t.text for t in s.toks]) for s in statements(text)]
    assert got[0] == (None, ["FILE", "IN1", "FB", "(", "80", "800", ")"])
    assert got[1] == ("CHECK", ["PROC"])
    assert got[2][1] == ["SQL", "INSERT", "INTO", "T", "VALUES", "(", "1", ")"]
    assert [g[1] for g in got[4:]] == [["A", "=", "1"], ["B", "=", "2"], ["C", "=", "3"]]


def test_files_and_fields():
    out = fixture()
    check_all(out)
    ents, rels = entities(out), relations(out)
    prog = ents[PROGRAM]
    assert prog["platform_kind"] == "ezt:Program" and prog["language"] == "easytrieve" and prog["activities"] == ["sort", "job"]
    assert prog["files"] == [{"dd": "CUSTIN", "file": "CUSTIN", "mode": "input"}, {"dd": "RPTOUT", "file": "RPTOUT", "mode": "output"}, {"dd": "SORTWK", "file": "SORTWK", "mode": "i-o"}]
    schema = key("DataSchema", kind="easytrieve-file", name="CUSTRPT.CUSTIN")
    assert ents[schema]["record_format"] == "FB" and ents[schema]["lrecl"] == 120 and ents[schema]["fields"] == 6
    assert (PROGRAM, "CONTAINS", schema) in rels
    bal = ents[key("Field", table="easytrieve:CUSTRPT.CUSTIN", name="CUST-BAL")]
    assert (bal["offset"], bal["length"], bal["type"], bal["usage"], bal["decimals"]) == (40, 7, "P", "packed-decimal", 2)
    assert (schema, "CONTAINS", key("Field", table="easytrieve:CUSTRPT.CUSTIN", name="CUST-BAL")) in rels
    # A printer file has no fields, so no DataSchema; working storage is not a file's field.
    assert key("DataSchema", kind="easytrieve-file", name="CUSTRPT.RPTOUT") not in ents
    assert not any(k[0] == "Field" and "WS-COUNT" in k[1] for k in ents)
    dd = {d["dd"]: d for d in of(out, "dd_file")}
    assert set(dd) == {"CUSTIN", "RPTOUT", "SORTWK"} and dd["SORTWK"]["record"] == "CUSTRPT.SORTWK" and dd["RPTOUT"]["record"] is None


def test_reports_are_produced():
    out = fixture()
    ents, rels = entities(out), relations(out)
    report = key("Report", platform="easytrieve", name="CUSTRPT.NORTHRPT")
    r = ents[report]
    assert r["printer"] == "RPTOUT" and r["control"] == ["SRT-REGION"] and r["sum"] == ["SRT-BAL", "WS-OVER"] and r["titles"] == 1 and r["job"] == "CUSTLIST"
    assert r["sequence"] == [{"field": "SRT-REGION", "desc": False}, {"field": "SRT-ID", "desc": False}]
    assert (PROGRAM, "PRODUCES", report) in rels
    assert "CUSTOMER BALANCES" not in str(r)  # title text is not copied
    later = fixture("commits/02-limit-report")
    assert entities(later)[key("Report", platform="easytrieve", name="CUSTRPT.LIMITRPT")]["summary"] is True


def test_the_sort_is_a_transform_shape():
    out = fixture()
    shapes = of(out, "transform")
    assert len(shapes) == 1 and shapes[0]["language"] == "easytrieve"
    assert validate_shape(shapes[0]) == []
    (t,) = shapes[0]["transforms"]
    assert t["op"] == "sort" and t["inputs"] == ["custin"] and t["output"] == "sortwk"
    assert t["order"] == [{"column": "CUST-REGION", "desc": False}, {"column": "CUST-BAL", "desc": True}]
    assert next(o for o in out.observations if o.kind == "transform").evidence_class == "E1"


def test_the_job_and_procedures_are_behaviour_units():
    out = fixture()
    us = {(u["owner"]["kind"], u["owner"]["name"]): u for u in units(out)}
    job = us[("Program", "CUSTRPT")]
    assert job["kind"] == "data_step"
    loop = job["body"][0]
    assert loop["op"] == "loop" and loop["kind"] == "implicit" and loop["over"] == {"e": "name", "name": "SORTWK"}
    assert job["body"][-1]["call"]["callee"]["name"] == "WRAPUP"  # FINISH(WRAPUP)
    ops = [n.get("op") for n in walk(job["body"])]
    for op in ("if", "continue", "assign", "call", "switch", "output", "query"):
        assert op in ops, op
    q = next(n for n in walk(job["body"]) if n.get("op") == "query")
    assert q["host_vars"] == {"read": ["SRT-ID", "WS-OVER"], "set": []}
    sw = next(n for n in walk(job["body"]) if n.get("op") == "switch")
    assert [m["value"] for m in sw["cases"][0]["match"]] == ["NE", "NW"] and "default" in sw
    assert us[("Symbol", "FLAG-OVER")]["kind"] == "function"
    rels = relations(out)
    flag = key("Symbol", platform="mainframe", program="CUSTRPT", name="FLAG-OVER")
    assert (PROGRAM, "CONTAINS", flag) in rels and (PROGRAM, "CALLS", flag) in rels
    assert rels[(PROGRAM, "CALLS", key("Program", platform="mainframe", name="AUDITLOG"))]["object"]["declare"] is False
    assert (PROGRAM, "WRITES_TO", key("Table", datastore="default", name="rpt_audit")) in rels


SAMPLE = """FILE MASTER VS
  M-KEY    1 10 A
  M-PART   M-KEY 4 A
  M-AMTS   * 5 P 2 OCCURS 12
  M-FLAG   * 1 A
%STDHDR
  M-LATE   * 2 N
FILE OUTFILE FB(80 800)
FILE WORKTAB TABLE INSTREAM
FILE VERYLONGNAME FB(80 800)
WS-I W 2 B 0
JOB INPUT NULL NAME LOADER
  GET MASTER
  IF EOF MASTER
    STOP
  END-IF
  DO WHILE WS-I LT 12
    WS-I = WS-I + 1
    IF M-FLAG EQ 'A' 'B' 'C'
      PUT OUTFILE FROM MASTER
    ELSE-IF M-FLAG NE 'X' AND M-KEY NOT NUMERIC
      WRITE MASTER UPDATE
    ELSE-IF M-LATE EQ 1 THRU 9
      DISPLAY OUTFILE M-KEY
    ELSE
      GOTO NEXTONE
    END-IF
  END-DO
  SQL PREPARE STMT1 FROM :WS-TEXT
  IF ) BAD
  END-IF
  PRINT
REPORT SUMMARY LINESIZE 80
  LINE M-KEY M-FLAG
BEFORE-BREAK. PROC
  DISPLAY 'BREAK'
END-PROC
JOB INPUT (MASTER KEY(M-KEY) OUTFILE KEY(M-KEY))
  IF MATCHED
    PERFORM NOPE
  END-IF
"""


def test_layouts_conditions_and_what_stays_unread():
    out = run("lib/T1.ezt", SAMPLE)
    check_all(out)
    ents = entities(out)
    f = lambda n: ents[key("Field", table="easytrieve:T1.MASTER", name=n)]  # noqa: E731
    assert f("M-PART")["offset"] == 0 and f("M-PART")["redefines"] == "M-KEY"
    assert f("M-AMTS")["occurs"] == 12 and f("M-FLAG")["offset"] == 70
    assert "offset" not in f("M-LATE")  # after a macro, the position is not known
    prog = ents[key("Program", platform="mainframe", name="T1")]
    assert {r["dd"]: r["mode"] for r in prog["files"]} == {"MASTER": "i-o", "OUTFILE": "i-o"}
    assert prog["reports"] == ["REPORT1"]
    assert key("Symbol", platform="mainframe", program="T1", name="REPORT1.BEFORE-BREAK") in ents
    us = units(out)
    loader = us[0]["body"]
    assert loader[0]["op"] == "platform" and loader[0]["construct"] == "ezt:get"
    cond = next(n for n in walk(loader) if n.get("op") == "if" and n["cond"].get("e") == "binop" and n["cond"]["op"] == "and" and n["cond"]["left"]["op"] == ">=")
    assert cond["cond"]["right"]["op"] == "<="  # THRU is a range
    assert any(n.get("op") == "if" and n["cond"] == {"e": "platform", "construct": "ezt:unparsed"} for n in walk(loader))
    assert us[1]["body"][0]["over"] == {"e": "array", "items": [{"e": "name", "name": "MASTER"}, {"e": "name", "name": "OUTFILE"}]}
    counts = constructs(out)
    for c in (("ezt:macro_call", False), ("ezt:file_special", False), ("ezt:sql_dynamic", False), ("ezt:synchronized_input", False), ("ezt:goto", False)):
        assert c in counts, c
    messages = " ".join(d.message for d in out.diagnostics)
    assert "VERYLONGNAME" in messages and "dynamic SQL" in messages and "PERFORM NOPE" in messages
    assert of(out, "lineage_summary")[0]["unresolved"] == 1


def test_output_is_deterministic():
    data = (FIXTURE / "base/ezt/CUSTRPT.ezt").read_bytes()
    a, b = run("x/CUSTRPT.ezt", data), run("x/CUSTRPT.ezt", data)
    assert [o.row() for o in a.observations] == [o.row() for o in b.observations]
