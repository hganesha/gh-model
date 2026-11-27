"""The assembler pack (repo_intel/platforms/hlasm/): the program, its calls, its DCB and ACB
files by DD name and its macros, at construct level, from one file's bytes."""

import hashlib
from pathlib import Path

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.platforms import CONSTRUCTS, packs, validate_pack
from repo_intel.platforms.hlasm import PACK
from repo_intel.platforms.hlasm.reader import split_operands, statements
from repo_intel.platforms.hlasm.vocabulary import LISTED_MACROS
from test_platform_pli import check_all, constructs, entities, key, of, relations, units

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "F41-pli-easytrieve"
DATEUTIL = key("Program", platform="mainframe", name="DATEUTIL")


def run(path: str, data: bytes | str):
    raw = data.encode() if isinstance(data, str) else data
    x = next(e for e in PACK.extractors if e.matches(path))
    return x.extract(path, raw, hashlib.sha256(raw).hexdigest())


def line(name: str = "", op: str = "", operands: str = "", cont: bool = False) -> str:
    s = f"{name:<8} {op:<5} {operands}".rstrip()
    return s.ljust(71) + "X" if cont else s


def test_the_pack_is_registered_and_valid():
    assert packs()["hlasm"] is PACK and PACK.prefix == "asm" and validate_pack(PACK) == []
    assert [x.id for x in PACK.extractors] == ["hlasm.source"]
    assert all(PACK.extractors[0].matches(p) for p in ("a/X.asm", "a/X.HLASM", "a/X.mac"))
    for m in LISTED_MACROS:
        assert f"asm:macro.{m}" in CONSTRUCTS


def test_reader_continuations_quotes_and_attributes():
    text = "\n".join([
        "* a comment",
        line("A", "DCB", "DDNAME=IN,DSORG=PS, remark", cont=True),
        " " * 15 + "MACRF=GM",
        line("", "MVC", "X(L'Y),=C'A B'") + " remark",
        line("", "DC", "C'IT''S'"),
    ]) + "\n"
    s = statements(text)
    assert [(x.name, x.op, x.operands) for x in s] == [("A", "DCB", "DDNAME=IN,DSORG=PS,MACRF=GM"), ("", "MVC", "X(L'Y),=C'A B'"), ("", "DC", "C'IT''S'")]
    assert split_operands("(A,(INPUT)),B,'X,Y'") == ["(A,(INPUT))", "B", "'X,Y'"]


def test_the_fixture_program():
    out = run("asm/DATEUTIL.asm", (FIXTURE / "base/asm/DATEUTIL.asm").read_bytes())
    check_all(out)
    assert units(out) == []
    ents, rels = entities(out), relations(out)
    prog = ents[DATEUTIL]
    assert prog["platform_kind"] == "asm:Program" and prog["language"] == "hlasm"
    assert prog["csects"] == ["DATEUTIL"] and prog["entries"] == ["DATENEXT"] and prog["externals"] == ["DATEFMT"]
    assert (prog["amode"], prog["rmode"]) == ("31", "ANY")
    assert prog["files"] == [{"file": "DATEIN", "dd": "DATEIN", "mode": "input"}]
    assert rels[(DATEUTIL, "CALLS", key("Program", platform="mainframe", name="CALCHOL"))]["object"]["declare"] is False
    assert key("Program", platform="mainframe", name="DATEFMT") not in {r[2] for r in rels}  # a V-con is not a call
    assert [(d["dd"], d["mode"], d["select"]) for d in of(out, "dd_file")] == [("DATEIN", "input", "DATEIN")]
    counts = constructs(out)
    for name in ("OPEN", "GET", "CLOSE", "WTO", "ABEND", "SAVE", "RETURN", "GETMAIN", "FREEMAIN"):
        assert sum(n for (c, _), n in counts.items() if c == f"asm:macro.{name}") == 1, name
    assert counts[("asm:call_dynamic", False)] == 1 and counts[("asm:machine_instruction", False)] == 6
    assert counts[("asm:link", True)] == 1 and counts[("asm:dcb", True)] == 1
    assert of(out, "lineage_summary")[0]["unresolved"] == 1
    assert "CL8'DATEFMT'" not in str([o.payload for o in out.observations])  # a DC value is not copied


SAMPLE = "\n".join([
    line("ORDPGM", "CSECT"),
    line("", "OPEN", "(INF,(INPUT),OUTF,(OUTPUT),VSF)"),
    line("", "CALL", "ORDSUB,(A,B),VL"),
    line("", "CALL", "(15)"),
    line("", "XCTL", "EP=ORDNEXT"),
    line("", "LINK", "EPLOC=NAMEFLD"),
    line("", "MYMACRO", "A,B"),
    line("", "EXEC", "CICS LINK PROGRAM('ORDCICS') COMMAREA(CA)"),
    line("", "EXEC", "CICS SEND MAP('ORDM1') MAPSET('ORDSET')"),
    line("", "EXEC", "SQL SELECT QTY INTO :QTY FROM ORDER_LINES", cont=True),
    " " * 15 + "WHERE ID = :ID",
    line("INF", "DCB", "DDNAME=ORDIN,MACRF=GM"),
    line("OUTF", "DCB", "DDNAME=ORDOUT,MACRF=PM"),
    line("VSF", "ACB", "MACRF=(KEY,OUT)"),
    line("NODD", "DCB", "MACRF=GM"),
    line("", "END"),
]) + "\n"


def test_calls_files_cics_and_sql():
    out = run("src/ORDPGM.asm", SAMPLE)
    check_all(out)
    ents, rels = entities(out), relations(out)
    prog = key("Program", platform="mainframe", name="ORDPGM")
    assert {f["dd"]: f["mode"] for f in ents[prog]["files"]} == {"ORDIN": "input", "ORDOUT": "output", "VSF": "i-o"}
    for name in ("ORDSUB", "ORDNEXT", "ORDCICS"):
        assert (prog, "CALLS", key("Program", platform="mainframe", name=name)) in rels, name
    assert (prog, "RENDERS", key("Screen", platform="bms", name="ORDSET.ORDM1")) in rels
    assert (prog, "READS_FROM", key("Table", datastore="default", name="order_lines")) in rels
    counts = constructs(out)
    assert counts[("asm:call_dynamic", False)] == 2 and counts[("asm:user_macro", False)] == 1 and counts[("asm:dcb", False)] == 1
    assert "NODD" in " ".join(d.message for d in out.diagnostics)


def test_a_macro_member_and_a_map_set_are_not_programs():
    macro = "\n".join([line("", "MACRO"), line("&L", "ORDMAC", "&P"), line("", "CALL", "INSIDE"), line("", "MEND")]) + "\n"
    out = run("maclib/ORDMAC.mac", macro)
    assert not any(k[0] == "Program" for k in entities(out)) and relations(out) == {}
    assert constructs(out) == {("asm:macro_definition", False): 1}
    bms = "\n".join([line("ORDSET", "DFHMSD", "TYPE=MAP"), line("ORDM1", "DFHMDI", "SIZE=(24,80)"), line("", "DFHMSD", "TYPE=FINAL"), line("", "END")]) + "\n"
    out = run("maps/ORDSET.mac", bms)
    assert not any(k[0] == "Program" for k in entities(out))
    assert constructs(out)[("asm:bms_macro", False)] == 3
