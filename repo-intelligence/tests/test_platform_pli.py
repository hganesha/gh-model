"""The PL/I pack (repo_intel/platforms/pli/): programs and %INCLUDE members read from one
file's bytes: the Program and its internal procedures with behaviour units, include members
as DataSchemas with Fields, files by DD name, calls, embedded SQL, DL/I and CICS."""

import hashlib
import json
from pathlib import Path

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import validate_unit, walk
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.platforms import CONSTRUCTS, packs, validate_pack
from repo_intel.platforms.pli import PACK
from repo_intel.platforms.pli.lexer import source

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "F41-pli-easytrieve"


def run(path: str, data: bytes | str):
    raw = data.encode() if isinstance(data, str) else data
    x = next(e for e in PACK.extractors if e.matches(path))
    return x.extract(path, raw, hashlib.sha256(raw).hexdigest())


def ref(x) -> tuple:
    return (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def key(_kind: str, **nk) -> tuple:
    return (_kind, json.dumps(nk, sort_keys=True))


def entities(out) -> dict[tuple, dict]:
    found: dict[tuple, dict] = {}
    for o in out.observations:
        if o.kind == "entity":
            found.setdefault(ref(o.payload), {}).update(o.payload["attributes"])
    return found


def relations(out) -> dict[tuple, dict]:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])): o.payload for o in out.observations if o.kind == "relation"}


def constructs(out) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = {}
    for o in out.observations:
        if o.kind == "construct":
            k = (o.payload["construct"], o.payload["modelled"])
            counts[k] = counts.get(k, 0) + 1
    return counts


def units(out) -> list[dict]:
    return [o.payload for o in out.observations if o.kind == "behavior"]


def of(out, kind: str) -> list[dict]:
    return [o.payload for o in out.observations if o.kind == kind]


def check_all(out) -> None:
    for u in units(out):
        assert validate_unit(u) == [], validate_unit(u)
        for node in walk(u["body"]):
            if node.get("op") == "platform" or node.get("e") == "platform":
                assert node["construct"] in CONSTRUCTS, node["construct"]
    for o in out.observations:
        if o.kind == "relation":
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p


PROGRAM = key("Program", platform="mainframe", name="CUSTUPD")


def test_the_pack_is_registered_and_valid():
    assert packs()["pli"] is PACK
    assert validate_pack(PACK) == []
    assert [x.id for x in PACK.extractors] == ["pli.source"]
    x = PACK.extractors[0]
    assert x.matches("src/CUSTUPD.pli") and x.matches("src/A.PL1") and not x.matches("src/a.pl")


def test_the_fixture_program_procedures_include_and_calls():
    out = run("pli/CUSTUPD.pli", (FIXTURE / "base/pli/CUSTUPD.pli").read_bytes())
    check_all(out)
    ents, rels = entities(out), relations(out)
    prog = ents[PROGRAM]
    assert prog["platform_kind"] == "pli:Program" and prog["language"] == "pli" and prog["main"] is True
    assert prog["files"] == [{"dd": "CUSTIN", "file": "CUSTIN", "mode": "input"}, {"dd": "CUSTNEW", "file": "CUSTOUT", "mode": "output"}, {"dd": "ERRRPT", "file": "ERRRPT", "mode": "output"}]
    validate = key("Symbol", platform="mainframe", program="CUSTUPD", name="VALIDATE")
    assert ents[validate]["params"] == ["REC", "RC"]
    assert (PROGRAM, "CONTAINS", validate) in rels and (PROGRAM, "CALLS", validate) in rels
    assert (PROGRAM, "DEPENDS_ON", key("DataSchema", kind="include", name="CUSTREC")) in rels
    callee = rels[(PROGRAM, "CALLS", key("Program", platform="mainframe", name="DATEUTIL"))]
    assert callee["object"]["declare"] is False
    accounts, run_log = key("Table", datastore="default", name="accounts"), key("Table", datastore="default", name="run_log")
    assert {(PROGRAM, "READS_FROM", accounts), (PROGRAM, "WRITES_TO", accounts), (PROGRAM, "WRITES_TO", run_log)} <= set(rels)
    assert key("Table", datastore="default", name="sqlca") not in ents
    assert of(out, "lineage_summary")[0]["unresolved"] == 0


def test_files_are_dd_file_observations():
    out = run("pli/CUSTUPD.pli", (FIXTURE / "base/pli/CUSTUPD.pli").read_bytes())
    dd = {d["dd"]: d for d in of(out, "dd_file")}
    assert set(dd) == {"CUSTIN", "CUSTNEW", "ERRRPT"}
    assert dd["CUSTNEW"]["select"] == "CUSTOUT" and dd["CUSTNEW"]["mode"] == "output"
    assert dd["CUSTIN"]["mode"] == "input" and dd["CUSTIN"]["record"] == "CUST_REC"
    assert all(d["program"]["natural_key"] == {"platform": "mainframe", "name": "CUSTUPD"} for d in dd.values())
    assert not any(o.kind == "relation" and o.payload["object"]["kind"] == "Dataset" for o in out.observations)


def test_behaviour_units_lower_the_logic():
    out = run("pli/CUSTUPD.pli", (FIXTURE / "base/pli/CUSTUPD.pli").read_bytes())
    us = {u["owner"]["name"]: u for u in units(out)}
    assert set(us) == {"CUSTUPD", "VALIDATE"} and all(u["kind"] == "function" for u in us.values())
    ops = [n.get("op") for n in walk(us["CUSTUPD"]["body"])]
    for op in ("loop", "switch", "query", "call", "transaction", "if"):
        assert op in ops, op
    q = next(n for n in walk(us["CUSTUPD"]["body"]) if n.get("op") == "query" and n["sql"].startswith("SELECT"))
    assert q["host_vars"] == {"read": ["CUST_ID"], "set": ["BAL"]}
    assert us["VALIDATE"]["params"] == [{"name": "REC"}, {"name": "RC"}]


def test_an_include_member_is_a_data_schema_with_fields():
    out = run("pli/include/CUSTREC.pli", (FIXTURE / "base/pli/include/CUSTREC.pli").read_bytes())
    check_all(out)
    ents, rels = entities(out), relations(out)
    schema = key("DataSchema", kind="include", name="CUSTREC")
    assert ents[schema]["layouts"] == ["CUST_REC"] and ents[schema]["fields"] == 10
    bal = ents[key("Field", table="pli:CUSTREC", name="CUST_BALANCE")]
    assert (bal["offset"], bal["length"], bal["usage"], bal["parent"]) == (106, 6, "FIXED DECIMAL", "CUST_REC")
    assert ents[key("Field", table="pli:CUSTREC", name="CUST_REC")]["length"] == 120
    assert ents[key("Field", table="pli:CUSTREC", name="POSTCODE")]["pic"] == "99999"
    assert (schema, "CONTAINS", key("Field", table="pli:CUSTREC", name="CITY")) in rels
    assert constructs(out)[("pli:unnamed_element", False)] == 1
    assert not any(k[0] == "Program" for k in ents)


def test_margins_follow_process_options():
    text = "*PROCESS MARGINS(5,40);\n" + "XXXX P: PROC OPTIONS(MAIN);".ljust(40) + "SEQ00010\n" + "XXXX  CALL OTHER;\nXXXX END P;\n"
    src = source(text)
    assert "XXXX" not in src.code and "SEQ" not in src.code
    out = run("a.pli", text)
    assert key("Program", platform="mainframe", name="P") in entities(out)
    assert (key("Program", platform="mainframe", name="P"), "CALLS", key("Program", platform="mainframe", name="OTHER")) in relations(out)
    # Default margins: columns 2 to 72, so a sequence number in 73 to 80 is not code.
    default = " Q: PROC OPTIONS(MAIN);".ljust(72) + "00000100\n END Q;\n"
    assert "00000100" not in source(default).code


SAMPLE = """ ORDDLI: PROC(PARMS) OPTIONS(MAIN);
    DCL PARMS CHAR(100) VAR;
    DCL PLITDLI ENTRY EXTERNAL;
    DCL HANDLER ENTRY VARIABLE;
    DCL THREE FIXED BIN(31) INIT(3);
    DCL GU CHAR(4) INIT('GU  ');
    DCL SSA1 CHAR(9) INIT('ORDSEG  ');
    DCL IOAREA CHAR(200);
    DCL PCB_PTR POINTER;
    DCL ORDFILE FILE RECORD UPDATE ENV(VB) TITLE(PARMS);
    CALL PLITDLI(THREE, GU, PCB_PTR, IOAREA, SSA1);
    CALL HANDLER(IOAREA);
    OPEN FILE(ORDFILE) UPDATE;
    EXEC CICS LINK PROGRAM('ORDCALC') COMMAREA(IOAREA);
    EXEC CICS SEND MAP('ORDM01') MAPSET('ORDSET') ERASE;
    EXEC SQL PREPARE S1 FROM :PARMS;
    DO I = 1 TO 10 BY 2;
       PUT SKIP LIST(I);
    END;
    RETURN;
 END ORDDLI;
"""


def test_dli_cics_and_dynamic_targets():
    out = run("src/ORDDLI.pli", SAMPLE)
    check_all(out)
    ents, rels = entities(out), relations(out)
    prog = key("Program", platform="mainframe", name="ORDDLI")
    assert ents[prog]["dli_segments"] == ["ORDSEG"] and ents[prog]["maps"] == ["ORDSET.ORDM01"]
    assert (prog, "CALLS", key("Program", platform="mainframe", name="ORDCALC")) in rels
    assert (prog, "RENDERS", key("Screen", platform="bms", name="ORDSET.ORDM01")) in rels
    # The ENTRY variable, the run-time TITLE and the PREPAREd SQL stay dynamic, with notes.
    assert not any(r[2][0] == "Program" and "HANDLER" in r[2][1] for r in rels)
    assert ("pli:call_dynamic", False) in constructs(out) and ("pli:exec_sql_dynamic", False) in constructs(out)
    assert of(out, "dd_file") == [] and of(out, "lineage_summary")[0]["unresolved"] == 1
    messages = " ".join(d.message for d in out.diagnostics)
    assert "TITLE is set at run time" in messages and "HANDLER" in messages


def test_an_unreadable_statement_is_counted_and_the_rest_lowered():
    text = " P: PROC OPTIONS(MAIN);\n    X = ;\n    DO WHILE(;\n       Y = 1;\n    END;\n    Z = 2;\n END P;\n"
    out = run("p.pli", text)
    check_all(out)
    assert constructs(out).get(("pli:unparsed", False), 0) >= 1
    body = units(out)[0]["body"]
    assert body[-1]["op"] == "assign" and body[-1]["target"] == {"e": "var", "name": "Z"}


def test_output_is_deterministic():
    data = (FIXTURE / "base/pli/CUSTUPD.pli").read_bytes()
    a, b = run("pli/CUSTUPD.pli", data), run("pli/CUSTUPD.pli", data)
    assert [o.row() for o in a.observations] == [o.row() for o in b.observations]
