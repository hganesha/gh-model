"""CICS BMS (repo_intel/platforms/bms): the assembler macro statement lexer, maps read into
Screens with a Form and the named fields as UIControls, literal fields as labels, and the
mapset's symbolic parameters counted, never resolved."""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.platforms import pack_extractors, packs, validate_pack
from repo_intel.platforms.bms import PACK, statements, vocabulary
from repo_intel.platforms.bms.source import BmsMaps, label_text, recognise
from repo_intel.platforms.bms.statements import Quoted, dynamic, lex, value, words

PACK_DIR = Path(__file__).resolve().parents[1] / "repo_intel" / "platforms" / "bms"


def card(text: str, continued: bool = False) -> str:
    """One source line: columns 1 to 71, and 'X' in column 72 when continued."""
    assert len(text) <= 71, text
    return text.ljust(71) + "X" if continued else text


def deck(*lines) -> bytes:
    return ("\n".join(card(*x) if isinstance(x, tuple) else card(x) for x in lines) + "\n").encode()


MAPSET = deck(
    "*  CUSTOMER MAPS",
    ("CUSTSET  DFHMSD TYPE=&SYSPARM,MODE=INOUT,LANG=COBOL,STORAGE=AUTO,", True),
    "               TIOAPFX=YES,CTRL=(FREEKB,FRSET)",
    "CUSTMAP  DFHMDI SIZE=(24,80),LINE=1,COLUMN=1,CURSLOC=YES",
    ("         DFHMDF POS=(1,25),LENGTH=16,ATTRB=(ASKIP,BRT),", True),
    "               INITIAL='CUSTOMER INQUIRY'",
    "         DFHMDF POS=(3,1),LENGTH=12,ATTRB=ASKIP,INITIAL='CUSTOMER NO:'",
    ("CUSTNO   DFHMDF POS=(3,14),LENGTH=8,ATTRB=(UNPROT,NUM,IC,FSET),", True),
    "               PICIN='9(8)'",
    "         DFHMDF POS=(3,23),LENGTH=1,ATTRB=ASKIP",
    "         DFHMDF POS=(5,1),LENGTH=5,ATTRB=ASKIP,INITIAL='NAME:'  LABEL",
    "CUSTNM   DFHMDF POS=(5,14),LENGTH=30,ATTRB=(ASKIP,NORM),INITIAL='-'",
    "CUSTBAL  DFHMDF POS=(6,14),LENGTH=12,PICOUT='ZZZ,ZZ9.99-',COLOR=RED",
    "MSGLINE  DFHMDF POS=(23,1),LENGTH=79,ATTRB=(ASKIP,BRT),HILIGHT=BLINK",
    "ORDLST   DFHMDI SIZE=(24,80),LINE=1,COLUMN=1,CTRL=FREEKB",
    "LINES    DFHMDF POS=(4,1),LENGTH=60,ATTRB=ASKIP,OCCURS=15",
    "SELNO    DFHMDF POS=(20,9),LENGTH=2,ATTRB=(UNPROT,NUM),VALIDN=MUSTFILL",
    "AMT1     DFHMDF POS=(21,1),LENGTH=5,GRPNAME=AMOUNT",
    "AMT2     DFHMDF POS=(21,6),LENGTH=5,GRPNAME=AMOUNT,XINIT=C1C2",
    "         DFHMSD TYPE=FINAL",
    "         END",
)
PATH = "cics/maps/CUSTSET.bms"


def run(data: bytes, path: str = PATH):
    return BmsMaps().extract(path, data, hashlib.sha256(data).hexdigest())


def key(kind: str, **nk) -> tuple:
    return (kind, json.dumps(nk, sort_keys=True))


def ref(x) -> tuple:
    return (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def entities(out) -> dict:
    return {key(o.payload["kind"], **o.payload["natural_key"]): o.payload["attributes"] for o in out.observations if o.kind == "entity"}


def relations(out) -> set:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])) for o in out.observations if o.kind == "relation"}


def constructs(out) -> Counter:
    return Counter((o.payload["construct"], o.payload["modelled"]) for o in out.observations if o.kind == "construct")


def screen(name):
    return key("Screen", platform="bms", name=name)


def control(container, name):
    return key("UIControl", container=container, name=name)


# ---------------------------------------------------------------------- the pack


def test_registered_as_the_bms_pack():
    assert packs()["bms"] is PACK and PACK.prefix == "bms" and validate_pack(PACK) == []
    assert [e.id for e in PACK.extractors] == ["bms.maps"]
    assert "bms.maps" in {e.id for e in pack_extractors()}
    # A map in a .mac or .asm member is not also read as assembler (contract 11.1 item 1).
    assert BmsMaps.supersedes_when_parsed == ("hlasm.source",)
    assert {t.name: t.core for t in PACK.terms} == {"bms:Map": EntityKind.SCREEN, "bms:MapForm": EntityKind.FORM, "bms:Field": EntityKind.UI_CONTROL}


def test_every_construct_and_term_the_reader_names_is_listed():
    listed = {c.id for c in vocabulary.CONSTRUCTS} | {t.name for t in vocabulary.TERMS}
    named = set()
    for path in PACK_DIR.glob("*.py"):
        named |= set(re.findall(r'"(bms:[A-Za-z_]+)"', path.read_text()))
    assert named <= listed, named - listed


def test_what_the_extractor_reads():
    e = BmsMaps()
    assert e.matches("a/CUSTSET.bms") and e.matches("maclib/CUSTSET.MAC") and e.matches("src/custset.asm")
    assert not e.matches("CUSTSET.cbl") and not e.matches("CUSTSET.jcl")
    # An assembler member that is not a map is left alone, without a diagnostic.
    plain = deck("PROG     CSECT", "         BR    14", "         END")
    out = run(plain, "src/prog.asm")
    assert out.parsed is False and not out.observations and not out.diagnostics
    out = run(plain, "maps/empty.bms")
    assert out.parsed is False and "DFHMSD" in out.diagnostics[0].message
    # A DFHMSD inside a comment does not make a file a map.
    assert not recognise("* DFHMSD TYPE=MAP\n         BR 14\n")
    assert recognise("CUSTSET  DFHMSD TYPE=MAP\n") and recognise("         dfhmsd type=final\n")


# ---------------------------------------------------------------------- the lexer


def test_continuation_in_column_72_resumes_in_column_16():
    src = deck(("LBL      DFHMSD TYPE=MAP,MODE=INOUT,", True), "               LANG=COBOL")
    [s] = lex(src.decode()).statements
    assert (s.label, s.op) == ("LBL", "DFHMSD")
    assert [(o.key, o.raw) for o in s.operands] == [("TYPE", "MAP"), ("MODE", "INOUT"), ("LANG", "COBOL")]
    assert s.start == 0 and s.end == len(src) - 1


def test_operands_end_at_a_blank_and_the_rest_is_remarks():
    src = deck(("         DFHMDF POS=(1,1),LENGTH=5    A REMARK THAT", True), "               GOES ON, NOT=OPERANDS", "X        DFHMDF POS=(2,1)")
    first, second = lex(src.decode()).statements
    assert [o.key for o in first.operands] == ["POS", "LENGTH"]
    assert second.label == "X" and second.value("POS") == ["2", "1"]


def test_a_quoted_string_may_run_on_past_column_71():
    text = "         DFHMDF POS=(1,1),INITIAL='A LONG TITLE THAT REACHES COLUMN 71."
    assert len(text) == 71
    src = deck((text, True), "               AND GOES ON',LENGTH=60")
    [s] = lex(src.decode()).statements
    assert s.value("INITIAL") == "A LONG TITLE THAT REACHES COLUMN 71.AND GOES ON"
    assert s.value("LENGTH") == "60"


def test_quotes_ampersands_comments_and_attribute_references():
    src = "* COMMENT\n.* MACRO COMMENT\nF DFHMDF INITIAL='IT''S A && B, (NOT A LIST)',LENGTH=L'FIELD,X=C'A'\n"
    [s] = lex(src).statements
    assert isinstance(s.value("INITIAL"), Quoted) and s.value("INITIAL") == "IT'S A & B, (NOT A LIST)"
    assert s.value("LENGTH") == "L'FIELD" and s.value("X") == "C'A'"


def test_values_lists_and_symbolic_parameters():
    assert value("(UNPROT,NUM,IC)") == ["UNPROT", "NUM", "IC"]
    assert value("((ORDER,SNGL),(PART,VIRTUAL,PARTDB))") == [["ORDER", "SNGL"], ["PART", "VIRTUAL", "PARTDB"]]
    assert value("(CODE,,'LIT')") == ["CODE", None, "LIT"]
    assert value("askip") == "ASKIP" and value("'Mixed Case'") == "Mixed Case"
    assert dynamic(value("&SYSPARM")) and dynamic(value("(A,&B)")) and not dynamic(value("'&&X'"))
    assert words(value("ASKIP")) == ["ASKIP"] and words(value("'TEXT'")) == []
    assert value("(" * 40 + "A" + ")" * 40)  # deep nesting stays bounded


def test_keyword_and_positional_operands_and_spans():
    src = "LBL      DFLD  'TEXT',POS=(1,2)\n"
    [s] = lex(src).statements
    assert [o.key for o in s.operands] == [None, "POS"]
    assert src[s.operands[1].start:s.operands[1].end] == "POS=(1,2)"
    assert s.positional()[0].value == "TEXT"


def test_a_label_alone_and_a_continuation_past_the_end_are_noted():
    lexed = lex("LONELY\n" + card("         DFHMDF POS=(1,1),", True) + "\n")
    assert len(lexed.statements) == 1
    assert any("no operation" in n for n in lexed.notes) and any("end of the file" in n for n in lexed.notes)


# ---------------------------------------------------------------------- maps


def test_maps_forms_and_fields():
    out = run(MAPSET)
    ents = entities(out)
    m = ents[screen("CUSTSET.CUSTMAP")]
    assert m["platform_kind"] == "bms:Map" and m["fidelity"] == "lossy"
    assert (m["mapset"], m["map"], m["size"], m["line"], m["column"]) == ("CUSTSET", "CUSTMAP", [24, 80], 1, 1)
    assert (m["mode"], m["lang"], m["storage"], m["tioapfx"], m["ctrl"], m["cursloc"]) == ("INOUT", "COBOL", "AUTO", "YES", ["FREEKB", "FRSET"], True)
    assert "type" not in m  # TYPE=&SYSPARM is decided when the map is assembled
    assert (m["fields"], m["literals"]) == (4, 3)
    assert ents[screen("CUSTSET.ORDLST")]["ctrl"] == ["FREEKB"]
    assert ents[key("Form", screen="CUSTSET.CUSTMAP", name="CUSTMAP")]["inputs"] == 1
    no = ents[control("CUSTSET.CUSTMAP", "CUSTNO")]
    assert no == {"pos": [3, 14], "length": 8, "attrb": ["UNPROT", "NUM", "IC", "FSET"], "control": "input", "picin": "9(8)",
                  "label": "CUSTOMER NO", "platform": "bms", "platform_kind": "bms:Field", "fidelity": "lossy"}
    nm = ents[control("CUSTSET.CUSTMAP", "CUSTNM")]
    assert (nm["control"], nm["label"], nm["initial"]) == ("display", "NAME", "-")
    bal = ents[control("CUSTSET.CUSTMAP", "CUSTBAL")]
    assert bal["picout"] == "ZZZ,ZZ9.99-" and bal["color"] == "RED" and "label" not in bal and "attrb" not in bal
    assert ents[control("CUSTSET.CUSTMAP", "MSGLINE")]["hilight"] == "BLINK"
    assert ents[control("CUSTSET.ORDLST", "LINES")]["occurs"] == 15
    assert ents[control("CUSTSET.ORDLST", "SELNO")]["validation"] == ["MUSTFILL"]
    assert ents[control("CUSTSET.ORDLST", "AMT1")]["group"] == "AMOUNT"
    assert ents[control("CUSTSET.ORDLST", "AMT2")]["initial_encoded"] == "hex"
    rels = relations(out)
    assert (screen("CUSTSET.CUSTMAP"), "CONTAINS", key("Form", screen="CUSTSET.CUSTMAP", name="CUSTMAP")) in rels
    assert (screen("CUSTSET.CUSTMAP"), "CONTAINS", control("CUSTSET.CUSTMAP", "CUSTNO")) in rels
    assert (screen("CUSTSET.ORDLST"), "CONTAINS", control("CUSTSET.ORDLST", "SELNO")) in rels
    for o in out.observations:
        if o.kind == "relation":
            assert check_relation(Predicate(o.payload["predicate"]), EntityKind(o.payload["subject"]["kind"]), EntityKind(o.payload["object"]["kind"])) is None



def test_the_initial_text_of_a_credential_field_is_never_copied():
    data = deck(
        "LOGSET   DFHMSD TYPE=&SYSPARM,MODE=INOUT,LANG=COBOL",
        "LOGMAP   DFHMDI SIZE=(24,80),LINE=1,COLUMN=1",
        "         DFHMDF POS=(3,1),LENGTH=9,ATTRB=ASKIP,INITIAL='PASSWORD:'",
        "PASSWD   DFHMDF POS=(3,14),LENGTH=8,ATTRB=(UNPROT,DRK),INITIAL='pz9x'",
        "USRTOKEN DFHMDF POS=(4,14),LENGTH=8,ATTRB=UNPROT,INITIAL='pz9y'",
        "TITLE    DFHMDF POS=(5,14),LENGTH=8,ATTRB=ASKIP,INITIAL='kept'",
        "         DFHMSD TYPE=FINAL",
    )
    out = run(data, "cics/maps/LOGSET.bms")
    blob = json.dumps([o.payload for o in out.observations])
    assert "pz9x" not in blob and "pz9y" not in blob
    ents = entities(out)
    pw = ents[control("LOGSET.LOGMAP", "PASSWD")]
    assert "initial" not in pw and pw["initial_redacted"] is True and pw["label"] == "PASSWORD"
    assert ents[control("LOGSET.LOGMAP", "USRTOKEN")]["initial_redacted"] is True
    assert ents[control("LOGSET.LOGMAP", "TITLE")]["initial"] == "kept"

def test_unnamed_fields_are_counted_and_never_declared():
    out = run(MAPSET)
    names = {json.loads(k[1])["name"] for k in entities(out) if k[0] == "UIControl"}
    assert names == {"CUSTNO", "CUSTNM", "CUSTBAL", "MSGLINE", "LINES", "SELNO", "AMT1", "AMT2"}
    c = constructs(out)
    # CUSTOMER NO: and NAME: label the fields after them; the title does not (CUSTOMER NO: follows it).
    assert c[("bms:literal_field", True)] == 2 and c[("bms:literal_field", False)] == 1
    assert c[("bms:unnamed_field", False)] == 1
    assert c[("bms:named_field", True)] == 8 and c[("bms:map", True)] == 2 and c[("bms:mapset", True)] == 1
    assert c[("bms:symbolic_parameter", False)] == 1
    assert c[("bms:cursor_location", True)] == 1 and c[("bms:occurs", True)] == 1 and c[("bms:group_field", True)] == 2
    assert c[("bms:validation", True)] == 1 and c[("bms:picture", True)] == 2 and c[("bms:extended_attributes", True)] == 2
    assert c[("bms:hex_or_graphic_initial", True)] == 1


def test_label_text_rule():
    assert label_text(Quoted("CUSTOMER NO....:")) == "CUSTOMER NO"
    assert label_text(Quoted("COMMAND ===>")) == "COMMAND"
    assert label_text(Quoted("----")) is None  # no letter: a rule, not a label
    assert label_text(Quoted("X" * 41)) is None  # longer than a label
    assert label_text("NOT_QUOTED") is None


def test_a_label_must_come_just_before_the_named_field():
    src = deck(
        "MS       DFHMSD TYPE=MAP,LANG=COBOL",
        "M1       DFHMDI SIZE=(24,80)",
        "         DFHMDF POS=(1,1),LENGTH=5,INITIAL='CODE:'",
        "         DFHMDF POS=(1,7),LENGTH=1",
        "F1       DFHMDF POS=(1,9),LENGTH=4",
        "         DFHMSD TYPE=FINAL",
    )
    ents = entities(run(src))
    assert "label" not in ents[control("MS.M1", "F1")]


def test_duplicate_field_names_get_a_suffix():
    src = deck("MS       DFHMSD TYPE=MAP", "M1       DFHMDI SIZE=(24,80)", "F1       DFHMDF POS=(1,1)", "F1       DFHMDF POS=(2,1)", "         DFHMSD TYPE=FINAL")
    ents = entities(run(src))
    assert control("MS.M1", "F1") in ents and ents[control("MS.M1", "F1~2")]["pos"] == [2, 1]


def test_maps_outside_a_mapset_and_unnamed_mapsets_are_not_read():
    src = deck("M1       DFHMDI SIZE=(24,80)", "F1       DFHMDF POS=(1,1)", "         DFHMSD TYPE=MAP", "M2       DFHMDI SIZE=(24,80)", "         DFHMSD TYPE=FINAL")
    out = run(src)
    assert not [k for k in entities(out)]
    c = constructs(out)
    assert c[("bms:map_without_mapset", False)] == 3 and c[("bms:mapset", False)] == 1
    assert any("without a mapset name" in d.message for d in out.diagnostics)


def test_other_statements_are_counted():
    src = deck("         PRINT NOGEN", "MS       DFHMSD TYPE=MAP", "         COPY  HEADERS", "         DFHPSD PARTNS=2", "         MNOTE 'X'", "         DFHMSD TYPE=FINAL", "         END")
    c = constructs(run(src))
    assert c[("bms:copy", False)] == 1 and c[("bms:partition_set", False)] == 1 and c[("bms:other_statement", False)] == 1


def test_spans_are_byte_spans():
    data = "* Kundenmasken üé\n".encode() + MAPSET
    out = run(data)
    [o] = [o for o in out.observations if o.kind == "entity" and o.payload["natural_key"].get("name") == "CUSTNO"]
    span = o.evidence.span
    assert data[span.start_byte:span.end_byte].decode().startswith("CUSTNO   DFHMDF POS=(3,14)")
    assert span.start_line == 9


def test_deterministic():
    a, b = run(MAPSET), run(MAPSET)
    assert [o.model_dump() for o in a.observations] == [o.model_dump() for o in b.observations]


def test_lexing_is_linear_on_a_long_file():
    big = deck("MS       DFHMSD TYPE=MAP", "M1       DFHMDI SIZE=(24,80)", *[f"F{i:06d}  DFHMDF POS=(1,1),LENGTH=1,INITIAL='{'A' * 25}'" for i in range(20000)], "         DFHMSD TYPE=FINAL")
    lexed = statements.lex(big.decode())
    assert len(lexed.statements) == 20003
