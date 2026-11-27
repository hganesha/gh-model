"""IMS (repo_intel/platforms/ims): DBD segments as Tables with their hierarchy and Fields,
PSBs as what their program may read and write, MFS message descriptors as Screens with
the device fields they map, and the stage-1 system definition's transactions as Endpoints;
and the F39 fixture, compiled."""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.platforms import pack_extractors, packs, validate_pack
from repo_intel.platforms.ims import PACK, vocabulary
from repo_intel.platforms.ims.psb import is_transaction_code
from repo_intel.platforms.ims.source import ImsSource, detect

PACK_DIR = Path(__file__).resolve().parents[1] / "repo_intel" / "platforms" / "ims"
U = "@unit"


def card(text: str, continued: bool = False) -> str:
    assert len(text) <= 71, text
    return text.ljust(71) + "X" if continued else text


def deck(*lines) -> bytes:
    return ("\n".join(card(*x) if isinstance(x, tuple) else card(x) for x in lines) + "\n").encode()


DBD = deck(
    "*  CUSTOMER DATABASE",
    "         DBD     NAME=CUSTDB,ACCESS=(HIDAM,VSAM),EXIT=(CAPTURE)",
    "         DATASET DD1=CUSTDD,DEVICE=3390",
    ("         SEGM    NAME=CUSTOMER,PARENT=0,BYTES=120,", True),
    "               POINTER=TWINBWD",
    "         LCHILD  NAME=(CUSTIDX,CUSTIDXD),POINTER=INDX",
    "         LCHILD  NAME=(CUSTSIX,CUSTSIXD),POINTER=INDX",
    "         XDFLD   NAME=XNAME,SRCH=(CUSTNAME),SEGMENT=CUSTOMER",
    "         FIELD   NAME=(CUSTNO,SEQ,U),BYTES=8,START=1,TYPE=C",
    "         FIELD   NAME=CUSTNAME,BYTES=30,START=9,TYPE=C",
    "         FIELD   NAME=/SX1",
    "         SEGM    NAME=ORDER,PARENT=CUSTOMER,BYTES=(60,20),COMPRTN=CMP1",
    "         FIELD   NAME=(ORDDATE,SEQ,M),BYTES=8,START=1,TYPE=C",
    "         FIELD   NAME=ORDDATE,BYTES=8,START=1",
    "         DATASET DD1=LINEDD",
    ("         SEGM    NAME=ORDLINE,", True),
    "               PARENT=((ORDER,SNGL),(PART,VIRTUAL,PARTDB))",
    "         FIELD   NAME=(LINENO,SEQ),BYTES=3,START=1,TYPE=Z",
    "         SEGM    NAME=ORDER,PARENT=CUSTOMER",
    "         DBDGEN",
    "         FINISH",
    "         END",
)

PSB = deck(
    "NOTEPCB  PCB    TYPE=TP,NAME=ORDNOTE,EXPRESS=YES",
    "         PCB    TYPE=TP,MODIFY=YES",
    "         PCB    TYPE=TP,LTERM=PRINTER01",
    "CUSTPCB  PCB    TYPE=DB,DBDNAME=CUSTDB,PROCOPT=A,KEYLEN=21",
    "         SENSEG NAME=CUSTOMER,PARENT=0,PROCOPT=G",
    "         SENFLD NAME=CUSTNO,START=1",
    "         SENSEG NAME=ORDER,PARENT=CUSTOMER",
    "         SENSEG NAME=ORDLINE,PARENT=ORDER,PROCOPT=K",
    "         PCB    TYPE=DB,DBDNAME=CUSTDB,PROCOPT=LS,PCBNAME=LOADPCB",
    "         SENSEG NAME=CUSTOMER,PARENT=0",
    "         PCB    TYPE=DB,DBDNAME=CUSTDB,PROCOPT=GO,PROCSEQ=CUSTIDXD",
    "         SENSEG NAME=CUSTOMER,PARENT=0",
    "         PCB    TYPE=GSAM,DBDNAME=EXTRACT,PROCOPT=LS",
    "         PCB    TYPE=DB,DBDNAME=&DB,PROCOPT=G",
    "         SENSEG NAME=X,PARENT=0",
    "         PSBGEN LANG=COBOL,PSBNAME=CUSTPGM,CMPAT=YES",
    "         END",
)

MFS = deck(
    "         PRINT  NOGEN",
    "CUSTF    FMT",
    ("         DEV    TYPE=(3270,2),FEAT=IGNORE,", True),
    "               PFK=(PFKEYS,'CUSTINQ ','CUSTUPD ')",
    "         DIV    TYPE=INOUT",
    "         DPAGE  CURSOR=((3,14))",
    "         DFLD   'CUSTOMER NO:',POS=(3,1)",
    "CUSTNO   DFLD   POS=(3,14),LTH=8,ATTR=(NUM,MOD)",
    "CUSTNM   DFLD   POS=(5,14),LTH=30,ATTR=(PROT)",
    "SPARE    DFLD   POS=(9,1),LTH=10",
    "TODAY    DFLD   POS=(1,70),LTH=8,ATTR=(PROT)",
    "         DEV    TYPE=3270P,WIDTH=132",
    "         DFLD   'CUSTOMER NO:',POS=(3,1)",
    "CUSTNO   DFLD   POS=(3,14),LTH=8",
    "         FMTEND",
    "CUSTIN   MSG    TYPE=INPUT,SOR=(CUSTF,IGNORE),NXT=CUSTOUT",
    "         SEG",
    "         MFLD   'CUSTINQ '",
    "         MFLD   PFKEYS,LTH=8",
    "         MFLD   (CUSTNO,'00000000'),LTH=8",
    "         MFLD   (,SCA)",
    "         MSGEND",
    "CUSTOUT  MSG    TYPE=OUTPUT,SOR=(CUSTF,IGNORE)",
    "         SEG",
    "         MFLD   (CUSTNO),LTH=8",
    "         MFLD   CUSTNM,LTH=30",
    "         MFLD   (TODAY,DATE2),LTH=8",
    "         MFLD   'NOT A CODE AT THE START OF AN OUTPUT MESSAGE'",
    "         DO     5",
    "         MFLD   NOWHERE,LTH=8",
    "         ENDDO",
    "         MSGEND",
    "ELSEIN   MSG    TYPE=INPUT,SOR=(OTHERF,IGNORE)",
    "         MFLD   AMOUNT,LTH=10",
    "         MSGEND",
    "         END",
)

STAGE1 = deck(
    "         IMSCTRL SYSTEM=(VS/2,(ALL,DB/DC)),IMSID=IMSA",
    ("         COMM    RECANY=(5,4096),APPLID=IMSAPPL1,", True),
    "               PASSWD=not-a-real-secret-0039",
    "         TRANSACT CODE=EARLY",
    "         DATABASE DBD=CUSTDB,ACCESS=UP",
    "         APPLCTN PSB=CUSTPGM,PGMTYPE=TP,SCHDTYP=PARALLEL",
    ("         TRANSACT CODE=CUSTINQ,MSGTYPE=(SNGLSEG,RESPONSE,1),", True),
    "               PRTY=(7,10,2),MODE=SNGL",
    ("         TRANSACT CODE=(CUSTUPD,CUSTUP2),", True),
    "               MSGTYPE=(MULTSEG,NONRESPONSE,1),SPA=80",
    "         APPLCTN GPSB=ORDGEN,PGMTYPE=TP,FPATH=YES",
    "         TRANSACT CODE=ORDFAST,MODE=MULT",
    "         TRANSACT CODE=&TRAN",
    "         RTCODE  CODE=ROUTE1",
    "         APPLCTN PSB=(&PSB),PGMTYPE=BATCH",
    "         END",
)
NEVER_COPIED = ("not-a-real-secret-0039", "IMSAPPL1", "IMSA", "4096")


def run(data: bytes, path: str):
    return ImsSource().extract(path, data, hashlib.sha256(data).hexdigest())


def key(kind: str, **nk) -> tuple:
    return (kind, json.dumps(nk, sort_keys=True))


def ref(x) -> tuple:
    return (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def entities(out) -> dict:
    return {key(o.payload["kind"], **o.payload["natural_key"]): o.payload["attributes"] for o in out.observations if o.kind == "entity"}


def relations(out) -> list:
    return [(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"]), o.payload["qualifiers"]) for o in out.observations if o.kind == "relation"]


def triples(out) -> set:
    return {r[:3] for r in relations(out)}


def constructs(out) -> Counter:
    return Counter((o.payload["construct"], o.payload["modelled"]) for o in out.observations if o.kind == "construct")


def segment(name, dbd="CUSTDB"):
    return key("Table", datastore=f"ims:{dbd}", name=name)


def field(seg, name, dbd="CUSTDB"):
    return key("Field", table=f"ims:{dbd}.{seg}", name=name)


def program(name):
    return key("Program", platform="mainframe", name=name)


def endpoint(code):
    return key("Endpoint", component_root=U, protocol="ims", method="TRANSACTION", route=code)


def check_relations(out):
    for o in out.observations:
        if o.kind == "relation":
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"])) is None, p


# ---------------------------------------------------------------------- the pack


def test_registered_as_the_ims_pack():
    assert packs()["ims"] is PACK and PACK.prefix == "ims" and validate_pack(PACK) == []
    assert [e.id for e in PACK.extractors] == ["ims.source"] and "ims.source" in {e.id for e in pack_extractors()}
    assert PACK.inputs == (".rkp/inputs/ims/**",)
    assert ImsSource.supersedes_when_parsed == ("hlasm.source",)
    assert {t.name: t.core for t in PACK.terms} == {
        "ims:Segment": EntityKind.TABLE, "ims:Field": EntityKind.FIELD, "ims:Message": EntityKind.SCREEN,
        "ims:DeviceField": EntityKind.UI_CONTROL, "ims:Transaction": EntityKind.ENDPOINT}
    assert all(t.fidelity == "lossy" for t in PACK.terms)


def test_every_construct_and_term_the_readers_name_is_listed():
    listed = {c.id for c in vocabulary.CONSTRUCTS} | {t.name for t in vocabulary.TERMS}
    named = set()
    for path in PACK_DIR.glob("*.py"):
        named |= set(re.findall(r'"(ims:[A-Za-z_]+)"', path.read_text()))
    assert named <= listed, named - listed


def test_files_are_recognised_by_content():
    e = ImsSource()
    for path in ("ims/CUSTDB.dbd", "ims/CUSTPGM.PSB", "ims/CUSTF.mfs", "src/x.asm", "maclib/x.mac", ".rkp/inputs/ims/stage1.gen"):
        assert e.matches(path), path
    assert not e.matches("CUSTSET.bms") and not e.matches("x.cbl") and not e.matches(".rkp/inputs/ims/.hidden")
    assert detect("ims/CUSTDB.dbd", DBD.decode()) == "dbd" and detect("lib/CUSTDB.asm", DBD.decode()) == "dbd"
    assert detect("ims/CUSTPGM.psb", PSB.decode()) == "psb" and detect("lib/CUSTPGM.mac", PSB.decode()) == "psb"
    assert detect("ims/CUSTF.mfs", MFS.decode()) == "mfs"
    assert detect(".rkp/inputs/ims/stage1.gen", STAGE1.decode()) == "sysdef"
    assert detect(".rkp/inputs/ims/CUSTDB.txt", DBD.decode()) == "dbd"
    # A BMS map or other assembler in .asm is not IMS; a DBD without DBDGEN in .asm is not taken.
    assert detect("src/map.asm", "MS       DFHMSD TYPE=MAP\n") is None
    assert detect("src/x.asm", "         DBD   NAME=X\n         SEGM  NAME=A\n") is None
    out = run(b"PROG     CSECT\n", "src/prog.asm")
    assert out.parsed is False and not out.diagnostics
    out = run(b"* nothing here\n", "ims/empty.dbd")
    assert out.parsed is False and "not an IMS source" in out.diagnostics[0].message


# ---------------------------------------------------------------------- DBD


def test_segments_hierarchy_and_fields():
    out = run(DBD, "ims/CUSTDB.dbd")
    ents = entities(out)
    cust = ents[segment("CUSTOMER")]
    assert cust["platform_kind"] == "ims:Segment" and cust["fidelity"] == "lossy"
    assert (cust["dbd"], cust["access"], cust["dd"], cust["level"], cust["bytes"], cust["pointer"]) == ("CUSTDB", ["HIDAM", "VSAM"], "CUSTDD", 1, 120, ["TWINBWD"])
    assert cust["lchild"] == [{"segment": "CUSTIDX", "dbd": "CUSTIDXD", "pointer": "INDX"}, {"segment": "CUSTSIX", "dbd": "CUSTSIXD", "pointer": "INDX"}]
    assert cust["secondary_indexes"] == [{"name": "XNAME", "search": ["CUSTNAME"], "source_segment": "CUSTOMER"}]
    order = ents[segment("ORDER")]
    assert (order["parent"], order["level"], order["bytes"], order["min_bytes"]) == ("CUSTOMER", 2, 60, 20)
    line = ents[segment("ORDLINE")]
    assert (line["parent"], line["level"], line["parent_pointer"], line["dd"]) == ("ORDER", 3, "SNGL", "LINEDD")
    assert line["logical_parent"] == {"segment": "PART", "dbd": "PARTDB", "pointer": "VIRTUAL"}
    assert ents[field("CUSTOMER", "CUSTNO")] == {"start": 1, "offset": 0, "length": 8, "type": "C", "key": True, "unique": True,
                                                 "platform": "ims", "platform_kind": "ims:Field", "fidelity": "lossy"}
    assert ents[field("ORDER", "ORDDATE")]["unique"] is False
    assert ents[field("ORDLINE", "LINENO")]["unique"] is True  # SEQ without U or M is unique
    assert not [k for k in ents if "/SX1" in k[1]]
    t = triples(out)
    assert (segment("CUSTOMER"), "CONTAINS", segment("ORDER")) in t and (segment("ORDER"), "CONTAINS", segment("ORDLINE")) in t
    assert (segment("CUSTOMER"), "CONTAINS", field("CUSTOMER", "CUSTNAME")) in t
    assert (segment("ORDLINE"), "CONTAINS", field("ORDLINE", "LINENO")) in t
    check_relations(out)


def test_dbd_constructs_and_what_is_not_read():
    out = run(DBD, "ims/CUSTDB.dbd")
    c = constructs(out)
    assert c[("ims:segment", True)] == 3 and c[("ims:segment", False)] == 1  # ORDER twice: the second is not read
    assert c[("ims:field", True)] == 4 and c[("ims:field", False)] == 1  # ORDDATE twice
    assert c[("ims:system_field", False)] == 1 and c[("ims:exit", False)] == 1 and c[("ims:compression", False)] == 1
    assert c[("ims:logical_child", True)] == 2 and c[("ims:secondary_index", True)] == 1 and c[("ims:logical_parent", True)] == 1
    assert c[("ims:dataset_group", True)] == 2 and c[("ims:dbd", True)] == 1
    messages = " ".join(d.message for d in out.diagnostics)
    assert "segment ORDER is defined twice" in messages and "field ORDDATE of segment ORDER is defined twice" in messages


def test_a_logical_dbd_and_names_that_are_not_literal():
    src = deck(
        "         FIELD   NAME=EARLY,BYTES=1,START=1",
        "         DBD     NAME=CUSTLDB,ACCESS=LOGICAL",
        "         DATASET LOGICAL",
        "         SEGM    NAME=CUSTOMER,SOURCE=((CUSTOMER,DATA,CUSTDB))",
        ("         SEGM    NAME=ORDPART,PARENT=CUSTOMER,", True),
        "               SOURCE=((ORDLINE,,CUSTDB),(PART,,PARTDB))",
        "         SEGM    NAME=&SEG,PARENT=CUSTOMER",
        "         SEGM    NAME=LOST,PARENT=NOSUCH",
        "         DBDGEN",
        "         DBD     NAME=&DBD",
        "         SEGM    NAME=ROOT,PARENT=0",
        "         DBDGEN",
    )
    out = run(src, "ims/CUSTLDB.dbd")
    ents = entities(out)
    assert ents[segment("CUSTOMER", "CUSTLDB")]["source"] == [{"segment": "CUSTOMER", "dbd": "CUSTDB"}]
    assert ents[segment("ORDPART", "CUSTLDB")]["source"] == [{"segment": "ORDLINE", "dbd": "CUSTDB"}, {"segment": "PART", "dbd": "PARTDB"}]
    assert ents[segment("CUSTOMER", "CUSTLDB")]["access"] == ["LOGICAL"]
    assert "level" not in ents[segment("LOST", "CUSTLDB")]
    assert segment("ROOT", "&DBD") not in ents and not [k for k in ents if "&" in k[1]]
    # LOST's parent is not declared here: the containment refers to it without declaring it.
    [lost] = [r for r in relations(out) if r[2] == segment("LOST", "CUSTLDB")]
    c = constructs(out)
    assert c[("ims:symbolic_parameter", False)] == 2 and c[("ims:segment_source", True)] == 2 and c[("ims:dbd", False)] == 1
    assert any("parent NOSUCH is not defined" in d.message for d in out.diagnostics)
    assert any("outside a segment" in d.message for d in out.diagnostics)
    obj = next(o for o in out.observations if o.kind == "relation" and o.payload["object"]["name"] == "LOST")
    assert obj.payload["subject"].get("declare") is False and lost


# ---------------------------------------------------------------------- PSB


def test_pcbs_are_what_the_program_may_read_and_write():
    out = run(PSB, "ims/CUSTPGM.psb")
    assert not [o for o in out.observations if o.kind == "entity"]  # the program and the segments are declared elsewhere
    rels = relations(out)
    got = sorted((p, json.loads(o[1])["name"], q["pcb"], q["procopt"]) for s, p, o, q in rels if p in ("READS_FROM", "WRITES_TO"))
    assert got == [
        ("READS_FROM", "CUSTOMER", "CUSTPCB", "G"),
        ("READS_FROM", "CUSTOMER", "pcb6", "GO"),
        ("READS_FROM", "ORDER", "CUSTPCB", "A"),
        ("WRITES_TO", "CUSTOMER", "LOADPCB", "LS"),
        ("WRITES_TO", "ORDER", "CUSTPCB", "A"),
    ]
    q = {(p, o, qq["pcb"]): qq for s, p, o, qq in rels}
    assert q[("READS_FROM", segment("CUSTOMER"), "CUSTPCB")]["fields"] == ["CUSTNO"]
    assert q[("READS_FROM", segment("CUSTOMER"), "pcb6")]["procseq"] == "CUSTIDXD"
    assert all(s == program("CUSTPGM") for s, *_ in rels)
    assert (program("CUSTPGM"), "CONSUMES", endpoint("ORDNOTE")) in triples(out)
    for o in out.observations:
        if o.kind == "relation":
            assert o.payload["subject"]["declare"] is False and o.payload["object"]["declare"] is False
    check_relations(out)


def test_psb_constructs():
    c = constructs(run(PSB, "ims/CUSTPGM.psb"))
    assert c[("ims:psbgen", True)] == 1 and c[("ims:db_pcb", True)] == 3 and c[("ims:db_pcb", False)] == 1
    assert c[("ims:tp_pcb", True)] == 1 and c[("ims:tp_pcb", False)] == 1  # PRINTER01 is longer than a transaction code
    assert c[("ims:modifiable_tp_pcb", False)] == 1 and c[("ims:gsam_pcb", False)] == 1
    assert c[("ims:key_sensitive_segment", False)] == 1 and c[("ims:sensitive_segment", True)] == 4 and c[("ims:sensitive_segment", False)] == 1
    assert c[("ims:sensitive_field", True)] == 1 and c[("ims:secondary_sequence", True)] == 1 and c[("ims:symbolic_parameter", False)] == 1


def test_pcbs_without_a_psbgen_name_no_program():
    src = deck("         PCB    TYPE=DB,DBDNAME=CUSTDB,PROCOPT=A", "         SENSEG NAME=CUSTOMER,PARENT=0", "         SENFLD NAME=CUSTNO,START=1")
    out = run(src, "ims/ORPHAN.psb")
    assert not relations(out)
    c = constructs(out)
    assert c == Counter({("ims:db_pcb", False): 1, ("ims:sensitive_segment", False): 1, ("ims:sensitive_field", False): 1})
    assert any("no PSBGEN" in d.message for d in out.diagnostics)
    out = run(deck("         PCB    TYPE=DB,DBDNAME=CUSTDB", "         SENSEG NAME=CUSTOMER", "         PSBGEN PSBNAME=&NAME"), "ims/DYN.psb")
    assert not relations(out) and constructs(out)[("ims:psbgen", False)] == 1


def test_transaction_codes():
    assert is_transaction_code("CUSTINQ") and is_transaction_code("A1#") and not is_transaction_code("PRINTER01")
    assert not is_transaction_code("1ABC") and not is_transaction_code("") and not is_transaction_code(None)


# ---------------------------------------------------------------------- MFS


def test_messages_are_screens_of_the_device_fields_they_map():
    out = run(MFS, "ims/CUSTF.mfs")
    ents = entities(out)
    mid = ents[key("Screen", platform="ims", name="CUSTIN")]
    assert (mid["message_type"], mid["format"], mid["devices"], mid["next"], mid["segments"], mid["fields"]) == ("input", "CUSTF", ["3270,2", "3270P"], "CUSTOUT", 1, 1)
    assert ents[key("UIControl", container="CUSTIN", name="CUSTNO")] == {
        "pos": [3, 14], "length": 8, "attr": ["NUM", "MOD"], "control": "input", "label": "CUSTOMER NO", "message_length": 8, "segment": 1,
        "initial": "00000000", "platform": "ims", "platform_kind": "ims:DeviceField", "fidelity": "lossy"}
    mod = ents[key("Screen", platform="ims", name="CUSTOUT")]
    assert mod["message_type"] == "output" and "next" not in mod and mod["fields"] == 3
    assert ents[key("UIControl", container="CUSTOUT", name="CUSTNM")]["control"] == "display"
    assert ents[key("UIControl", container="CUSTOUT", name="TODAY")]["system_literal"] == "DATE2"
    # A device field no message names, PF key fields and fields of a format not in the file
    assert key("UIControl", container="CUSTIN", name="PFKEYS") not in ents and not [k for k in ents if "SPARE" in k[1]]
    assert ents[key("UIControl", container="ELSEIN", name="AMOUNT")] == {"message_length": 10, "segment": 1, "platform": "ims", "platform_kind": "ims:DeviceField", "fidelity": "lossy"}
    t = triples(out)
    assert (key("Screen", platform="ims", name="CUSTIN"), "NAVIGATES_TO", endpoint("CUSTINQ")) in t
    assert (key("Screen", platform="ims", name="CUSTIN"), "NAVIGATES_TO", key("Screen", platform="ims", name="CUSTOUT")) in t
    assert (key("Screen", platform="ims", name="CUSTOUT"), "CONTAINS", key("UIControl", container="CUSTOUT", name="CUSTNO")) in t
    assert not [r for r in t if r[0] == key("Screen", platform="ims", name="CUSTOUT") and r[1] == "NAVIGATES_TO"]
    check_relations(out)


def test_mfs_constructs():
    out = run(MFS, "ims/CUSTF.mfs")
    c = constructs(out)
    assert c[("ims:format", True)] == 1 and c[("ims:device", True)] == 2 and c[("ims:pf_keys", False)] == 1
    assert c[("ims:input_message", True)] == 2 and c[("ims:output_message", True)] == 1
    assert c[("ims:message_field", True)] == 5  # CUSTNO in; CUSTNO, CUSTNM and TODAY out; AMOUNT
    # PFKEYS, the control area and a field naming no device field
    assert c[("ims:system_message_field", False)] == 3
    assert c[("ims:literal_message_field", True)] == 1 and c[("ims:literal_message_field", False)] == 1
    # CUSTNO, CUSTNM and TODAY are mapped, SPARE is not; the printer device's CUSTNO repeats a name
    assert c[("ims:device_field", True)] == 3 and c[("ims:device_field", False)] == 2
    assert c[("ims:literal_device_field", True)] == 1 and c[("ims:literal_device_field", False)] == 1
    assert c[("ims:repeated_fields", False)] == 1 and c[("ims:next_message", True)] == 1
    assert any("format OTHERF is not in this file" in d.message for d in out.diagnostics)


# ---------------------------------------------------------------------- stage 1


def test_transactions_are_endpoints_the_application_exposes():
    out = run(STAGE1, ".rkp/inputs/ims/stage1.gen")
    ents = entities(out)
    inq = ents[endpoint("CUSTINQ")]
    assert inq == {"psb": "CUSTPGM", "pgmtype": "TP", "message_segments": "single", "response": True, "mode": "single",
                   "platform": "ims", "platform_kind": "ims:Transaction", "fidelity": "lossy"}
    assert ents[endpoint("CUSTUPD")]["conversational"] is True and ents[endpoint("CUSTUP2")]["response"] is False
    fast = ents[endpoint("ORDFAST")]
    assert (fast["psb"], fast["generated_psb"], fast["fast_path"], fast["mode"]) == ("ORDGEN", True, True, "multiple")
    assert "psb" not in ents[endpoint("EARLY")]
    assert set(ents) == {endpoint(c) for c in ("EARLY", "CUSTINQ", "CUSTUPD", "CUSTUP2", "ORDFAST")}
    t = triples(out)
    assert t == {(program("CUSTPGM"), "EXPOSES", endpoint("CUSTINQ")), (program("CUSTPGM"), "EXPOSES", endpoint("CUSTUPD")),
                 (program("CUSTPGM"), "EXPOSES", endpoint("CUSTUP2")), (program("ORDGEN"), "EXPOSES", endpoint("ORDFAST"))}
    assert all(str(o.evidence_class) == "E1" for o in out.observations)
    c = constructs(out)
    # one TRANSACT names CUSTUPD and CUSTUP2; CODE=&TRAN is set when the deck is assembled
    assert c[("ims:transaction", True)] == 4 and c[("ims:transaction", False)] == 1
    assert c[("ims:application", True)] == 2 and c[("ims:application", False)] == 1
    assert c[("ims:sysdef_other", False)] == 2 and c[("ims:database", False)] == 1 and c[("ims:routing_code", False)] == 1
    assert c[("ims:conversational", True)] == 1 and c[("ims:fast_path", True)] == 1
    check_relations(out)


def test_system_definition_values_are_never_copied():
    out = run(STAGE1, ".rkp/inputs/ims/stage1.gen")
    text = json.dumps([o.payload for o in out.observations]) + json.dumps([d.message for d in out.diagnostics])
    for needle in NEVER_COPIED:
        assert needle not in text, needle


# ---------------------------------------------------------------------- general


def test_spans_are_byte_spans():
    data = "* Kundendatenbank ü\n".encode() + DBD
    out = run(data, "ims/CUSTDB.dbd")
    [o] = [o for o in out.observations if o.kind == "entity" and o.payload["natural_key"].get("name") == "CUSTNAME"]
    span = o.evidence.span
    assert data[span.start_byte:span.end_byte].decode().strip().startswith("FIELD   NAME=CUSTNAME")


def test_deterministic():
    for data, path in ((DBD, "a.dbd"), (PSB, "a.psb"), (MFS, "a.mfs"), (STAGE1, ".rkp/inputs/ims/s.gen")):
        a, b = run(data, path), run(data, path)
        assert [o.model_dump() for o in a.observations] == [o.model_dump() for o in b.observations]


# ---------------------------------------------------------------------- the fixture, compiled


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    from repo_intel.compile.fixtures import materialize
    from repo_intel.compile.pipeline import compile_commit, load_shards
    from repo_intel.storage.store import ObjectStore

    root = tmp_path_factory.mktemp("F39")
    sha = materialize("F39-bms-ims", root / "repo")[0]
    store = ObjectStore(root / "store", "t")
    result = compile_commit(store, root / "repo", "github.com/1001", sha)
    shards = load_shards(store, result.manifest)
    return {"entities": {e["id"]: e for s in shards.values() for e in s["entities"]}, "assertions": [a for s in shards.values() for a in s["assertions"]],
            "coverage": store.get_json(result.manifest.coverage_ref), "status": result.manifest.status}


def test_coverage_of_bms_and_ims_constructs(compiled):
    cov = {c["scope"]: c for c in compiled["coverage"] if c["domain"] == "platform_constructs" and c["scope"] in ("bms", "ims")}
    assert (cov["bms"]["numerator"], cov["bms"]["denominator"]) == (20, 26)
    assert (cov["ims"]["numerator"], cov["ims"]["denominator"]) == (51, 59)
    assert "bms:partition_set" in cov["bms"]["detail"]["not_seen"] and "ims:gsam_pcb" in cov["ims"]["detail"]["not_seen"]


def test_the_psb_and_the_system_definition_link_to_the_program_the_repository_holds(compiled):
    assert compiled["status"] == "complete"
    ents = compiled["entities"]
    named = lambda e: (e["kind"], e["name"])
    links = {(named(ents[a["subject"]]), a["predicate"], named(ents[a["object"]])) for a in compiled["assertions"]
             if a["predicate"] in ("READS_FROM", "WRITES_TO", "EXPOSES", "CONSUMES")}
    assert links == {
        (("Program", "CUSTPGM"), "READS_FROM", ("Table", "CUSTOMER")), (("Program", "CUSTPGM"), "READS_FROM", ("Table", "ORDER")),
        (("Program", "CUSTPGM"), "READS_FROM", ("Table", "ORDLINE")), (("Program", "CUSTPGM"), "WRITES_TO", ("Table", "ORDER")),
        (("Program", "CUSTPGM"), "WRITES_TO", ("Table", "ORDLINE")),
        (("Program", "CUSTPGM"), "EXPOSES", ("Endpoint", "TRANSACTION CUSTINQ")), (("Program", "CUSTPGM"), "EXPOSES", ("Endpoint", "TRANSACTION CUSTUPD")),
        (("Program", "CUSTPGM"), "CONSUMES", ("Endpoint", "TRANSACTION ORDNOTE")),
    }
    # ORDPGM is named by the system definition only: nothing declares it, so nothing exposes ORDNOTE.
    assert not [e for e in ents.values() if e["kind"] == "Program" and e["name"] == "ORDPGM"]
