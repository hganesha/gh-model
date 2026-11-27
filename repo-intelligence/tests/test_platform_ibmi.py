"""The IBM i platform pack (repo_intel/platforms/ibmi): RPG III and IV in fixed, free and
mixed form with embedded SQL (`ibmi.rpg`), CL (`ibmi.cl`) and DDS physical, logical,
display and printer files (`ibmi.dds`)."""

import ast
import hashlib
import json
import re
import time
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import validate_unit
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.compile.transforms import validate_shape
from repo_intel.extractors import detect_language
from repo_intel.platforms import packs, validate_pack
from repo_intel.platforms.ibmi import PACK, IbmiCl, IbmiDds, IbmiRpg
from repo_intel.platforms.ibmi import cl as clmod
from repo_intel.platforms.ibmi import sql as embedded
from repo_intel.platforms.ibmi.rpglex import tokenize

FIXTURES = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures"
F40 = FIXTURES / "F40-ibmi-rpg" / "base"
PACK_DIR = Path(__file__).resolve().parents[1] / "repo_intel" / "platforms" / "ibmi"
SOURCES = [
    "qddssrc/CUSTMAST.pf",
    "qddssrc/CUSTL1.lf",
    "qddssrc/CUSTDSP.dspf",
    "qddssrc/CUSTRPT.prtf",
    "qrpglesrc/CUSTINQ.rpgle",
    "qrpglesrc/CUSTUPD.sqlrpgle",
    "qclsrc/CUSTJOB.clle",
]


# ------------------------------------------------------------------ helpers


def extractor_for(path: str):
    return next(e for e in PACK.extractors if e.matches(path))


def run(text: str | bytes, path: str):
    data = text if isinstance(text, bytes) else text.encode()
    return extractor_for(path).extract(path, data, hashlib.sha256(data).hexdigest())


def fixture(path: str):
    return run((F40 / path).read_bytes(), path)


def ref(x) -> tuple:
    return x if isinstance(x, str) else (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def key(entity_kind: str, **nk) -> tuple:
    return (entity_kind, json.dumps(nk, sort_keys=True))


def prog(name: str) -> tuple:
    return key("Program", platform="mainframe", name=name)


def sym(program: str, name: str) -> tuple:
    return key("Symbol", platform="mainframe", program=program, name=name)


def table(name: str) -> tuple:
    return key("Table", datastore="default", name=name)


def entities(out) -> dict[tuple, dict]:
    found: dict[tuple, dict] = {}
    for o in out.observations:
        if o.kind == "entity":
            found.setdefault(ref(o.payload), {}).update(o.payload["attributes"])
    return found


def relations(out) -> set[tuple]:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])) for o in out.observations if o.kind == "relation"}


def relation_rows(out) -> list[dict]:
    return [o.payload for o in out.observations if o.kind == "relation"]


def constructs(out) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = {}
    for o in out.observations:
        if o.kind == "construct":
            k = (o.payload["construct"], o.payload["modelled"])
            counts[k] = counts.get(k, 0) + 1
    return counts


def units(out) -> dict[str, dict]:
    return {o.payload["owner"]["name"]: o.payload for o in out.observations if o.kind == "behavior"}


def messages(out) -> list[str]:
    return [d.message for d in out.diagnostics]


def walk(block):
    for stmt in block:
        yield stmt
        for k in ("then", "else", "body", "default", "finally"):
            if isinstance(stmt.get(k), list):
                yield from walk(stmt[k])
        for case in stmt.get("cases", []):
            yield from walk(case.get("body", []))
        for catch in stmt.get("catches", []):
            yield from walk(catch.get("body", []))


def ops(unit: dict) -> list[str]:
    return [s["op"] if s["op"] != "platform" else s["construct"] for s in walk(unit["body"])]


def cols(*pairs) -> str:
    """A fixed-form line: (column, text) pairs, 1-based."""
    line = [" "] * 80
    for c, t in pairs:
        for i, ch in enumerate(t):
            line[c - 1 + i] = ch
    return "".join(line).rstrip()


def c4(f1="", op="", f2="", res="", ind="", rind="", ext=None) -> str:
    """An RPG IV C-spec."""
    pairs = [(6, "C")] + ([(9, ind)] if ind else []) + ([(12, f1)] if f1 else []) + ([(26, op)] if op else [])
    if ext is not None:
        pairs.append((36, ext))
    else:
        pairs += ([(36, f2)] if f2 else []) + ([(50, res)] if res else []) + ([(71, rind)] if rind else [])
    return cols(*pairs)


def c3(f1="", op="", f2="", res="", ind="", rind="", ln="", dec="") -> str:
    """An RPG III C-spec: indicators 9-17, factor 1 18-27, opcode 28-32, factor 2 33-42,
    result 43-48, length 49-51, decimals 52, resulting indicators 54-59."""
    pairs = [(6, "C")] + ([(9, ind)] if ind else []) + ([(18, f1)] if f1 else []) + ([(28, op)] if op else []) + ([(33, f2)] if f2 else [])
    pairs += ([(43, res)] if res else []) + ([(52 - len(ln), ln)] if ln else []) + ([(52, dec)] if dec else []) + ([(54, rind)] if rind else [])
    return cols(*pairs)


def a(name="", nt="", ln="", typ="", dec="", use="", row="", col="", kw="", ref_="", cond="") -> str:
    """A DDS line."""
    pairs = [(6, "A")] + ([(7, cond)] if cond else []) + ([(17, nt)] if nt else []) + ([(19, name)] if name else []) + ([(29, ref_)] if ref_ else [])
    pairs += ([(35 - len(ln), ln)] if ln else []) + ([(35, typ)] if typ else []) + ([(38 - len(dec), dec)] if dec else []) + ([(38, use)] if use else [])
    pairs += ([(42 - len(row), row)] if row else []) + ([(45 - len(col), col)] if col else []) + ([(45, kw)] if kw else [])
    return cols(*pairs)


RPG3 = "\n".join([
    cols((6, "F"), (7, "ORDHDR"), (15, "U"), (16, "F"), (19, "E"), (31, "K"), (40, "DISK"), (66, "A")),
    cols((6, "F"), (7, "ORDPRT"), (15, "O"), (16, "F"), (19, "E"), (40, "PRINTER")),
    c3("*ENTRY", "PLIST"),
    c3(op="PARM", res="ORDNO", ln="7", dec="0"),
    c3("ORDNO", "CHAIN", "ORDHDR", rind="90"),
    c3("*IN90", "IFEQ", "'0'"),
    c3(op="Z-ADD", f2="0", res="TOTAL", ln="9", dec="2"),
    c3(op="EXSR", f2="CALC"),
    c3(op="UPDAT", f2="ORDREC"),
    c3(op="WRITE", f2="PRTLINE"),
    c3(op="END"),
    c3(op="CALL", f2="'ORDLOG'"),
    c3(op="PARM", res="ORDNO"),
    c3(op="SETON", rind="    LR", ind=" 90"),
    c3(op="RETRN"),
    c3("CALC", "BEGSR"),
    c3(op="ADD", f2="1", res="TOTAL"),
    c3(op="ENDSR"),
]) + "\n"

MODULE = """**FREE
ctl-opt nomain;

dcl-pr GetRate packed(5:2) extproc('RATES_GET');
  code char(3) const;
end-pr;
dcl-pr RunIt extpgm(pgmName);
end-pr;
dcl-pr Audit extpgm('AUDITLOG') end-pr;
dcl-s pgmName char(10);

dcl-proc CalcTax export;
  dcl-pi *n packed(9:2);
    amount packed(9:2) const;
  end-pi;
  dcl-s rate packed(5:2);

  rate = GetRate('VAT');
  exsr Round;
  RunIt();
  Audit();
  select;
    when amount > 1000;
      rate += 1;
    when amount < 0;
      return 0;
    other;
      rate = rate;
  endsl;
  for i = 1 to 10 by 2;
    iter;
  endfor;
  monitor;
    rate = rate / 0;
  on-error 0102;
    rate = 0;
  endmon;
  return amount * rate;

  begsr Round;
    rate = %dech(rate : 5 : 2);
  endsr;
end-proc;

dcl-proc Helper;
  CalcTax(1);
  Missing(1 : 2);
end-proc;
"""

MIXED = "\n".join([
    cols((6, "F"), (7, "CUSTMAST"), (17, "U"), (18, "F"), (20, "A"), (22, "E"), (34, "K"), (36, "DISK")),
    cols((6, "D"), (7, "pgmVar"), (24, "S"), (39, "10"), (40, "A")),
    cols((7, "/FREE")),
    "        chain custNo CUSTMAST;",
    "        if %found(CUSTMAST);",
    "          update CUSTREC;",
    "        endif;",
    cols((7, "/END-FREE")),
    cols((6, "C"), (7, "/EXEC SQL")),
    cols((6, "C"), (7, "+ DELETE FROM CUSTHIST WHERE CUSNO = :custNo")),
    cols((6, "C"), (7, "/END-EXEC")),
    c4(op="CALLB", f2="'LOGIT'"),
    c4(op="CALL", f2="pgmVar"),
    c4(op="EVAL", ext="*INLR = *ON"),
]) + "\n"

CL_MORE = """             PGM
             DCLF       FILE(ORDDSP)
             DCL        VAR(&I) TYPE(*INT)
             DCL        VAR(&SQL) TYPE(*CHAR) LEN(200)
             DOFOR      VAR(&I) FROM(1) TO(3)
                CALLSUBR   SUBR(SHOW)
             ENDDO
             SELECT
             WHEN       COND(&I *GT 2) THEN(CHGVAR VAR(&I) VALUE(0))
             OTHERWISE  CMD(CHGVAR VAR(&I) VALUE(&I + 1))
             ENDSELECT
             CPYF       FROMFILE(ORDLIB/ORDERS) TOFILE(ORDLIB/ORDBAK) MBROPT(*REPLACE)
             CLRPFM     FILE(ORDWORK)
             RUNSQL     SQL('INSERT INTO ORDLOG SELECT * FROM ORDERS') COMMIT(*NONE)
             RUNSQL     SQL(&SQL) COMMIT(*NONE)
             CALLPRC    PRC('ORD_NOTIFY')
             TFRCTL     PGM(ORDEND)
             SUBR       SUBR(SHOW)
                SNDRCVF    RCDFMT(ORDPNL)
             ENDSUBR
             ENDPGM
"""


# ------------------------------------------------------------------ the pack


def test_pack_is_valid_and_registered():
    assert validate_pack(PACK) == []
    assert packs()["ibmi"] is PACK
    assert PACK.prefix == "ibmi" and PACK.version == "1.1.0" and PACK.supersedes == ()
    assert sorted(e.id for e in PACK.extractors) == ["ibmi.cl", "ibmi.dds", "ibmi.rpg"]
    for path, lang in (("a.rpg", "rpg"), ("a.rpgle", "rpg"), ("a.sqlrpgle", "rpg"), ("a.clp", "cl"), ("a.clle", "cl"), ("a.pf", "dds"), ("a.lf", "dds"), ("a.dspf", "dds"), ("a.prtf", "dds")):
        assert detect_language(path) == lang
        assert extractor_for(path) is not None


def test_modules_import_the_extractors_package_only_inside_functions():
    for py in PACK_DIR.glob("*.py"):
        tree = ast.parse(py.read_text())
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("repo_intel.extractors"), f"{py.name} imports {node.module} at module level"
            if isinstance(node, ast.Import):
                assert not any(n.name.startswith("repo_intel.extractors") for n in node.names), py.name


def test_every_term_and_construct_named_in_the_code_is_in_the_vocabulary():
    names = {t.name for t in PACK.terms} | {c.id for c in PACK.constructs}
    for py in PACK_DIR.glob("*.py"):
        if py.name == "vocabulary.py":
            continue
        for found in re.findall(r"""["'](ibmi:[A-Za-z_]+)["']""", py.read_text()):
            assert found in names, f"{py.name}: {found} is not in the vocabulary"


def test_no_banned_words_in_the_pack():
    # Prose only: RPG's UNLOCK opcode is a name, written in upper case.
    banned = re.compile(r"\b(delve|harness|illuminate|underscore|tapestry|pivotal|crucial|profound|empower|elevate|unlock)\b")
    for py in list(PACK_DIR.glob("*.py")) + [Path(__file__)]:
        text = py.read_text()
        if py == Path(__file__):
            text = text.replace(banned.pattern, "")
        assert not banned.search(text), f"{py.name}: {banned.search(text).group()}"


# ------------------------------------------------------------------ every output is valid


ALL = [(p, (F40 / p).read_bytes()) for p in SOURCES] + [("x/ORD3.rpg", RPG3.encode()), ("x/TAXMOD.rpgle", MODULE.encode()), ("x/MIXED.sqlrpgle", MIXED.encode()), ("x/ORDCL.clp", CL_MORE.encode())]


@pytest.mark.parametrize("path,data", ALL, ids=[p for p, _ in ALL])
def test_behaviour_relations_and_shapes_are_valid(path, data):
    out = run(data, path)
    assert out.parsed
    for o in out.observations:
        p = o.payload
        if o.kind == "behavior":
            assert validate_unit(p) == [], (p["owner"]["name"], validate_unit(p))
            assert p["language"] == "ibmi"
        elif o.kind == "relation":
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p
        elif o.kind == "transform":
            assert validate_shape(p) == []


@pytest.mark.parametrize("path,data", ALL, ids=[p for p, _ in ALL])
def test_output_is_deterministic(path, data):
    first = [(o.kind, json.dumps(o.payload, sort_keys=True), o.evidence.span.start_byte) for o in run(data, path).observations]
    second = [(o.kind, json.dumps(o.payload, sort_keys=True), o.evidence.span.start_byte) for o in run(data, path).observations]
    assert first == second


def test_spans_are_byte_spans_inside_the_file():
    for path, data in ALL:
        for o in run(data, path).observations:
            s = o.evidence.span
            assert 0 <= s.start_byte <= s.end_byte <= len(data), (path, o.kind)


# ------------------------------------------------------------------ RPG


def test_fixed_form_program_keys_files_and_screens():
    out = fixture("qrpglesrc/CUSTINQ.rpgle")
    ents, rels = entities(out), relations(out)
    me = ents[prog("CUSTINQ")]
    assert me["platform_kind"] == "ibmi:RpgProgram" and me["language"] == "rpg" and me["rpg_version"] == "IV" and me["form"] == ["fixed"]
    assert me["params"] == ["pcust"]
    assert ents[sym("CUSTINQ", "INIT")]["symbol_kind"] == "subroutine"
    assert (prog("CUSTINQ"), "CONTAINS", sym("CUSTINQ", "INIT")) in rels
    assert (prog("CUSTINQ"), "CALLS", sym("CUSTINQ", "INIT")) in rels
    assert (prog("CUSTINQ"), "READS_FROM", table("custmast")) in rels
    assert (prog("CUSTINQ"), "READS_FROM", key("DataSchema", kind="ibmi-lf", name="custmast")) in rels
    assert not any(r[1] == "WRITES_TO" for r in rels)  # an input file
    for fmt in ("CUSTINQ1", "CUSTINQ2"):
        assert (prog("CUSTINQ"), "RENDERS", key("Screen", platform="ibmi", name=f"CUSTDSP.{fmt}")) in rels
        assert (key("Form", screen=f"CUSTDSP.{fmt}", name=fmt), "SUBMITS_TO", prog("CUSTINQ")) in rels
    # What a file is cannot be known from the program: neither reading is declared.
    for row in relation_rows(out):
        if row["object"]["kind"] in ("Table", "DataSchema", "Screen"):
            assert row["object"].get("declare") is False


def test_fixed_form_behaviour():
    us = units(fixture("qrpglesrc/CUSTINQ.rpgle"))
    main, init = us["CUSTINQ"], us["INIT"]
    assert init["kind"] == "paragraph" and "kind" not in main
    assert main["params"][0]["name"] == "pcust"
    assert ops(main) == ["declare", "call", "loop", "ibmi:exfmt", "if", "break", "ibmi:chain", "if", "ibmi:exfmt", "assign", "assign"]
    loop = next(s for s in walk(main["body"]) if s["op"] == "loop")
    assert loop["kind"] == "while" and loop["cond"] == {"e": "unop", "op": "not", "operand": {"e": "var", "scope": "indicator", "name": "*in03"}}
    chain = next(s for s in walk(main["body"]) if s.get("construct") == "ibmi:chain")
    assert chain["detail"] == {"opcode": "CHAIN", "file": "CUSTMAST", "key": {"e": "var", "name": "cusno"}, "indicators": {"hi": "90"}}
    assert ops(init) == ["assign", "ibmi:other_opcode"]


def test_free_form_sqlrpgle_with_a_procedure():
    out = fixture("qrpglesrc/CUSTUPD.sqlrpgle")
    ents, rels, us = entities(out), relations(out), units(out)
    me = ents[prog("CUSTUPD")]
    assert me["form"] == ["free", "fully_free"] and me["sql"] is True and me["params"] == ["pcust", "plimit"]
    assert ents[sym("CUSTUPD", "LOGCHANGE")]["symbol_kind"] == "procedure"
    assert (prog("CUSTUPD"), "CALLS", prog("CUSTLOG")) in rels
    assert (prog("CUSTUPD"), "CALLS", sym("CUSTUPD", "LOGCHANGE")) in rels
    assert (prog("CUSTUPD"), "READS_FROM", table("custmast")) in rels
    assert (prog("CUSTUPD"), "WRITES_TO", table("custmast")) in rels
    assert (sym("CUSTUPD", "LOGCHANGE"), "WRITES_TO", table("custhist")) in rels
    assert (prog("CUSTUPD"), "PRODUCES", key("Report", platform="ibmi", name="CUSTRPT")) in rels
    assert us["LOGCHANGE"]["kind"] == "function" and [p["name"] for p in us["LOGCHANGE"]["params"]] == ["cust", "amount"]
    select = next(s for s in walk(us["CUSTUPD"]["body"]) if s["op"] == "query")
    assert select["sql"] == "SELECT CUSNAME FROM CUSTMAST WHERE CUSNO = :custNo"
    assert select["host_vars"] == {"read": ["custno"], "set": ["custname"]}
    assert select["target"] == {"e": "var", "name": "custname"}
    insert = next(s for s in walk(us["LOGCHANGE"]["body"]) if s["op"] == "query")
    assert insert["host_vars"] == {"read": ["cust", "amount"]}


def test_rpg3_fixed_form():
    out = run(RPG3, "x/ORD3.rpg")
    ents, rels, us = entities(out), relations(out), units(out)
    assert ents[prog("ORD3")]["rpg_version"] == "III"
    assert (prog("ORD3"), "READS_FROM", table("ordhdr")) in rels and (prog("ORD3"), "WRITES_TO", table("ordhdr")) in rels
    assert (prog("ORD3"), "PRODUCES", key("Report", platform="ibmi", name="ORDPRT")) in rels
    assert (prog("ORD3"), "CALLS", prog("ORDLOG")) in rels
    assert (prog("ORD3"), "CALLS", sym("ORD3", "CALC")) in rels
    main = us["ORD3"]
    assert main["params"][0]["name"] == "ordno"
    assert ops(main) == ["ibmi:chain", "if", "assign", "call", "ibmi:update", "ibmi:write", "call", "if", "assign", "return"]
    call = next(s for s in walk(main["body"]) if s["op"] == "call" and s["call"]["callee"].get("kind") == "program")
    assert call["call"]["callee"]["name"] == "ORDLOG" and call["call"]["args"] == [{"e": "var", "name": "ordno"}]
    cond = next(s for s in walk(main["body"]) if s["op"] == "if" and s["then"][0]["op"] == "assign" and s["then"][0]["target"].get("scope") == "indicator")
    assert cond["cond"] == {"e": "var", "scope": "indicator", "name": "*in90"}
    assert ops(us["CALC"]) == ["assign"]
    assert us["CALC"]["body"][0]["value"]["op"] == "+"


def test_module_procedures_exports_and_local_subroutines():
    out = run(MODULE, "x/TAXMOD.rpgle")
    ents, rels, us = entities(out), relations(out), units(out)
    assert ents[prog("TAXMOD")]["module"] is True
    calc = sym("TAXMOD", "CALCTAX")
    assert ents[calc]["exported"] is True and ents[calc]["returns"] == "packed(9:2)"
    alias = sym("*EXPORT", "CALCTAX")
    assert ents[alias]["platform_kind"] == "ibmi:ExportedProcedure"
    assert (alias, "ALIAS_OF", calc) in rels
    assert ents[sym("TAXMOD", "CALCTAX.ROUND")]["procedure"] == "CALCTAX"
    assert (calc, "CALLS", sym("TAXMOD", "CALCTAX.ROUND")) in rels
    assert (calc, "CALLS", sym("*EXPORT", "RATES_GET")) in rels  # EXTPROC's literal
    assert (calc, "CALLS", prog("AUDITLOG")) in rels
    assert (sym("TAXMOD", "HELPER"), "CALLS", calc) in rels
    # RunIt's EXTPGM names a variable; Missing has no prototype here: neither is guessed.
    assert not any("RUNIT" in str(r) or "MISSING" in str(r).upper() for r in rels)
    counts = constructs(out)
    assert counts[("ibmi:call_dynamic", False)] == 1 and counts[("ibmi:call_unresolved", False)] == 1
    assert any("run time" in m for m in messages(out)) and any(m.startswith("missing is called") for m in messages(out))
    body = ops(us["CALCTAX"])
    assert body[:3] == ["declare", "assign", "call"] and "switch" not in body and "try" in body and "loop" in body and "continue" in body
    assert us["CALCTAX.ROUND"]["kind"] == "paragraph"
    assert [p["name"] for p in us["CALCTAX"]["params"]] == ["amount"]


def test_mixed_form_with_fixed_exec_sql_and_dynamic_call():
    out = run(MIXED, "x/MIXED.sqlrpgle")
    ents, rels, us = entities(out), relations(out), units(out)
    assert ents[prog("MIXED")]["form"] == ["fixed", "free"]
    for p in ("READS_FROM", "WRITES_TO"):
        assert (prog("MIXED"), p, table("custmast")) in rels
    assert (prog("MIXED"), "WRITES_TO", table("custhist")) in rels
    assert (prog("MIXED"), "CALLS", sym("*EXPORT", "LOGIT")) in rels
    assert not any(r[1] == "CALLS" and "pgmvar" in r[2][1].lower() for r in rels)
    assert constructs(out)[("ibmi:call_dynamic", False)] == 1
    body = ops(us["MIXED"])
    assert body == ["declare", "ibmi:chain", "if", "ibmi:update", "query", "call", "call", "assign"]
    query = next(s for s in walk(us["MIXED"]["body"]) if s["op"] == "query")
    assert query["sql"] == "DELETE FROM CUSTHIST WHERE CUSNO = :custNo"


def test_extfile_naming_a_variable_stays_dynamic():
    src = "**FREE\ndcl-s fileName char(21);\ndcl-f ORDERS disk usage(*update) extfile(fileName);\ndcl-f HIST disk usage(*output) extfile('PRODLIB/ORDHIST');\nread ORDERS;\n"
    out = run(src, "x/EXTF.rpgle")
    rels = relations(out)
    assert not any(r[2] == table("orders") for r in rels)
    assert (prog("EXTF"), "WRITES_TO", table("ordhist")) in rels
    assert constructs(out)[("ibmi:extfile_dynamic", False)] == 1


def test_embedded_sql_statements():
    st = embedded.analyse(" SELECT A, B INTO :x, :ds.y FROM LIB/T1 JOIN T2 ON T1.K = T2.K WHERE C = :c")
    assert st.construct == "ibmi:exec_sql_select_into" and st.reads == ["c"] and st.sets == ["x", "ds.y"]
    assert "INTO" not in st.sql and "LIB.T1" in st.lineage and ":c" not in st.lineage
    assert len(st.lineage) == len(" SELECT A, B INTO :x, :ds.y FROM LIB/T1 JOIN T2 ON T1.K = T2.K WHERE C = :c")
    cur = embedded.analyse(" DECLARE C1 CURSOR FOR SELECT * FROM ORDERS")
    assert cur.cursor == "c1" and cur.sql == "SELECT * FROM ORDERS"
    dyn = embedded.analyse(" PREPARE S1 FROM :stmt")
    assert dyn.construct == "ibmi:exec_sql_dynamic" and dyn.dynamic_from == "stmt"
    src = "**FREE\ndcl-s stmt varchar(200);\nexec sql PREPARE S1 FROM :stmt;\nexec sql EXECUTE S1;\nexec sql DECLARE C2 CURSOR FOR SELECT ID FROM ITEMS;\nexec sql OPEN C2;\nexec sql FETCH C2 INTO :id;\nexec sql CLOSE C2;\nexec sql COMMIT;\n"
    out = run(src, "x/SQLS.sqlrpgle")
    body = units(out)["SQLS"]["body"]
    assert [s["op"] for s in body] == ["declare", "query", "platform", "query", "platform", "platform", "transaction"]
    assert body[1]["dynamic"] is True
    assert body[3]["sql"] == "SELECT ID FROM ITEMS" and body[3]["target"] == {"e": "var", "scope": "cursor", "name": "c2"}
    assert body[4]["host_vars"] == {"set": ["id"]}
    assert (prog("SQLS"), "READS_FROM", table("items")) in relations(out)
    assert any("built at run time" in m for m in messages(out))


def test_tokens_and_specials():
    toks = tokenize("x = *blanks + %trim(y) ; *in03 = *on", 0, 36)
    assert [t.kind for t in toks][:3] == ["name", "op", "special"]


def test_reader_is_linear():
    lines = [c4(op="EVAL", ext=f"X{i % 50} = X{i % 50} + {i}") for i in range(20000)]
    src = "\n".join(lines) + "\n"
    start = time.perf_counter()
    out = run(src, "x/BIG.rpgle")
    assert time.perf_counter() - start < 30
    assert len(units(out)["BIG"]["body"]) == 20000
    free = "**FREE\n" + "".join(f"DoIt{i % 7}(x : y);\n" for i in range(20000))
    start = time.perf_counter()
    out = run(free, "x/CALLS.rpgle")
    assert time.perf_counter() - start < 30
    assert len(units(out)["CALLS"]["body"]) == 20000
    cl_text = "PGM\n" + "".join(f"  CALL PGM(P{i % 9}) PARM(&A)\n" for i in range(20000)) + "ENDPGM\n"
    start = time.perf_counter()
    run(cl_text, "x/MANY.clp")
    assert time.perf_counter() - start < 30
    unterminated = "**FREE\n" + "x = 'abc" * 20000
    start = time.perf_counter()
    run(unterminated, "x/BAD.rpgle")
    assert time.perf_counter() - start < 30


def test_names_never_values():
    src = "**FREE\ndcl-c DB_PASSWORD 'fixture-not-a-real-password-0040';\ndcl-s token char(40) inz('fixture-token-0040');\nCallIt(DB_PASSWORD);\n"
    out = run(src, "x/SECRETS.rpgle")
    blob = json.dumps([o.payload for o in out.observations])
    assert "fixture-not-a-real-password-0040" not in blob
    assert "fixture-token-0040" not in blob
    blob = json.dumps([o.payload for o in fixture("qclsrc/CUSTJOB.clle").observations])
    assert "FIXTUSR40" not in blob and "FIXTVAL40" not in blob
    blob = json.dumps([o.payload for o in fixture("qrpglesrc/CUSTUPD.sqlrpgle").observations])
    assert "fixture-not-a-real-password-0040" not in blob and "ftpPassword".upper() in blob.upper()


# ------------------------------------------------------------------ CL


def test_cl_program_calls_submits_and_overrides():
    out = fixture("qclsrc/CUSTJOB.clle")
    ents, rels, us = entities(out), relations(out), units(out)
    me = ents[prog("CUSTJOB")]
    assert me["platform_kind"] == "ibmi:ClProgram" and me["language"] == "cl" and me["params"] == ["cust", "limit"] and me["monmsg"] == 1
    assert me["overrides"][0] == {"command": "OVRDBF", "file": "CUSTMAST", "tofile": "CUSTMAST", "library": "CUSTLIB", "line": 11}
    assert [d["op"] for d in me["data_areas"]] == ["retrieve", "retrieve", "change"]
    assert (prog("CUSTJOB"), "CALLS", prog("CUSTINQ")) in rels and (prog("CUSTJOB"), "CALLS", prog("CUSTUPD")) in rels
    vias = sorted(r["qualifiers"]["via"] for r in relation_rows(out) if r["predicate"] == "CALLS")
    assert vias == ["call", "call", "sbmjob"]
    # The override names a file, but no relation guesses which call it affects.
    assert not any(r[2] == table("custmast") for r in rels)
    assert constructs(out)[("ibmi:cl_call_dynamic", False)] == 1 and constructs(out)[("ibmi:cl_monmsg", False)] == 1
    body = us["CUSTJOB"]["body"]
    branch = next(s for s in body if s["op"] == "if" and "else" in s)
    assert branch["then"][0]["construct"] == "ibmi:cl_sbmjob" and branch["else"][0]["op"] == "call"
    assert any("&NEXTPGM" in m for m in messages(out))


def test_cl_subroutines_loops_files_sql_and_copies():
    out = run(CL_MORE, "x/ORDCL.clp")
    ents, rels, us = entities(out), relations(out), units(out)
    assert ents[sym("ORDCL", "SHOW")]["symbol_kind"] == "subroutine"
    assert (prog("ORDCL"), "CALLS", sym("ORDCL", "SHOW")) in rels
    assert (prog("ORDCL"), "CALLS", sym("*EXPORT", "ORD_NOTIFY")) in rels
    assert (prog("ORDCL"), "CALLS", prog("ORDEND")) in rels
    assert (prog("ORDCL"), "READS_FROM", table("orders")) in rels and (prog("ORDCL"), "WRITES_TO", table("ordbak")) in rels
    assert (prog("ORDCL"), "WRITES_TO", table("ordwork")) in rels and (prog("ORDCL"), "WRITES_TO", table("ordlog")) in rels
    assert (sym("ORDCL", "SHOW"), "RENDERS", key("Screen", platform="ibmi", name="ORDDSP.ORDPNL")) in rels
    assert (key("Form", screen="ORDDSP.ORDPNL", name="ORDPNL"), "SUBMITS_TO", prog("ORDCL")) in rels
    assert not any(r[2] == table("orddsp") for r in rels)  # a display file is not read as a table
    shapes = [o.payload for o in out.observations if o.kind == "transform"]
    assert shapes and shapes[0]["transforms"][0]["inputs"] == ["orders"] and shapes[0]["transforms"][0]["output"] == "ordbak"
    main = us["ORDCL"]
    assert [s["op"] for s in main["body"]][:4] == ["declare", "declare", "loop", "if"]
    assert main["body"][2]["kind"] == "for" and main["body"][2]["body"][0]["op"] == "call"
    assert main["body"][3]["else"][0]["value"] == {"e": "binop", "op": "+", "left": {"e": "var", "name": "i"}, "right": {"e": "lit", "value": 1, "type": "integer"}}
    queries = [s for s in walk(main["body"]) if s["op"] == "query"]
    assert queries[0]["sql"].startswith("INSERT INTO ORDLOG") and queries[1]["dynamic"] is True
    assert us["SHOW"]["kind"] == "paragraph"


def test_cl_text_handling():
    text = "  CHGVAR  VAR(&A) +\n            VALUE('x') /* a comment */\n  CALL PGM(LIB/P1) -\nPARM(&A)\n"
    masked = clmod.mask(text)
    assert len(masked) == len(text) and "comment" not in masked
    cmds = [clmod.parse_command(masked, a, b) for a, b in clmod._split_commands(masked)]
    assert [c.name for c in cmds] == ["CHGVAR", "CALL"] and cmds[1].value("PARM") == "&A"
    positional = clmod.parse_command("CALL P2 (&X &Y)", 0, 15)
    assert positional.value("PGM") == "P2" and positional.value("PARM") == "&X &Y"


# ------------------------------------------------------------------ DDS


def test_physical_file_fields_keys_and_values():
    out = fixture("qddssrc/CUSTMAST.pf")
    ents, rels = entities(out), relations(out)
    t = ents[table("custmast")]
    assert t["platform_kind"] == "ibmi:PhysicalFile" and t["record_format"] == "CUSTREC" and t["keys"] == ["cusno"] and t["unique"] is True
    cusno = ents[key("Field", table="custmast", name="cusno")]
    assert cusno["type"] == "DECIMAL(7, 0)" and cusno["bytes"] == 4 and cusno["offset"] == 0 and cusno["key"] == 1 and cusno["colhdg"] == "Customer Number"
    assert ents[key("Field", table="custmast", name="cusstat")]["offset"] == 39
    cl = key("CodeList", platform="ibmi", name="custmast.cusstat")
    assert ents[cl]["values"] == ["A", "I", "H"]
    assert (key("Field", table="custmast", name="cusstat"), "BINDS_TO", cl) in rels
    assert sum(1 for r in rels if r[1] == "CONTAINS") == 4


def test_logical_file():
    out = fixture("qddssrc/CUSTL1.lf")
    ents, rels = entities(out), relations(out)
    lf = ents[key("DataSchema", kind="ibmi-lf", name="custl1")]
    assert lf["based_on"] == ["custmast"] and lf["keys"] == ["cusname"]
    assert lf["select_omit"] == [{"type": "select", "field": "cusstat", "tests": ["COMP(EQ 'A')"]}]
    assert (key("DataSchema", kind="ibmi-lf", name="custl1"), "CONTAINS", key("Field", table="custmast", name="cusname")) in rels


def test_join_logical_file_and_continued_keywords():
    src = "\n".join([
        a(kw="JDFTVAL"),
        a(nt="R", name="ORDJ", kw="JFILE(ORDHDR ORDLIN)"),
        a(nt="J", kw="JOIN(ORDHDR ORDLIN)"),
        a(kw="JFLD(ORDNO ORDNO)"),
        a(name="ORDNO", kw="JREF(1)"),
        a(name="ITEM"),
        a(name="QTY", kw="TEXT('Quantity +"),
        a(kw="ordered')"),
    ]) + "\n"
    out = run(src, "x/ORDJL.lf")
    ents, rels = entities(out), relations(out)
    lf = ents[key("DataSchema", kind="ibmi-lf", name="ordjl")]
    assert lf["based_on"] == ["ordhdr", "ordlin"] and lf["join"] is True
    assert not any(r[1] == "CONTAINS" for r in rels)  # a join's fields cannot be placed in one file
    assert constructs(out)[("ibmi:dds_join", True)] >= 2


def test_display_file_screens_forms_and_controls():
    out = fixture("qddssrc/CUSTDSP.dspf")
    ents, rels = entities(out), relations(out)
    s1 = key("Screen", platform="ibmi", name="CUSTDSP.CUSTINQ1")
    assert ents[s1]["command_keys"] == ["CA03"] and ents[s1]["constants"] == 2
    f1 = key("Form", screen="CUSTDSP.CUSTINQ1", name="CUSTINQ1")
    assert (s1, "CONTAINS", f1) in rels
    ctl = key("UIControl", container="CUSTDSP.CUSTINQ1", name="CUSNO")
    assert ents[ctl]["row"] == 5 and ents[ctl]["column"] == 20 and ents[ctl]["usage"] == "both"
    assert (f1, "CONTAINS", ctl) in rels
    assert (s1, "CONTAINS", key("UIControl", container="CUSTDSP.CUSTINQ1", name="MSG")) in rels
    assert (key("UIControl", container="CUSTDSP.CUSTINQ2", name="CUSLIMIT"), "BINDS_TO", key("Field", table="custmast", name="cuslimit")) in rels
    assert (key("UIControl", container="CUSTDSP.CUSTINQ2", name="CUSSTAT"), "BINDS_TO", key("CodeList", platform="ibmi", name="CUSTDSP.CUSTINQ2.CUSSTAT")) in rels
    assert ents[key("UIControl", container="CUSTDSP.CUSTINQ2", name="MSG")]["conditioned"] is True
    assert not any(k[0] == "UIControl" and '"name": ""' in k[1] for k in ents)  # constants are counted, not declared


def test_subfile_display_records():
    src = "\n".join([
        a(kw="REF(ORDHDR)"),
        a(nt="R", name="ORDSFL", kw="SFL"),
        a(name="ORDNO", ref_="R", use="O", row="5", col="2"),
        a(nt="R", name="ORDCTL", kw="SFLCTL(ORDSFL)"),
        a(kw="SFLSIZ(20) SFLPAG(10)"),
        a(kw="CF05(05 'Refresh')"),
        a(row="1", col="2", kw="DATE"),
    ]) + "\n"
    out = run(src, "x/ORDDSP.dspf")
    ents, rels = entities(out), relations(out)
    assert ents[key("Screen", platform="ibmi", name="ORDDSP.ORDSFL")]["subfile"] is True
    ctl = ents[key("Screen", platform="ibmi", name="ORDDSP.ORDCTL")]
    assert ctl["subfile_control"] == "ORDSFL" and ctl["command_keys"] == ["CF05"] and ctl["unnamed_fields"] == 1
    assert (key("UIControl", container="ORDDSP.ORDSFL", name="ORDNO"), "BINDS_TO", key("Field", table="ordhdr", name="ordno")) in rels
    assert key("Form", screen="ORDDSP.ORDSFL", name="ORDSFL") not in ents  # output only: no form


def test_printer_file_report():
    ents = entities(fixture("qddssrc/CUSTRPT.prtf"))
    rpt = ents[key("Report", platform="ibmi", name="CUSTRPT")]
    assert rpt["platform_kind"] == "ibmi:PrinterFile"
    assert rpt["record_formats"] == [{"name": "RPTHEAD", "fields": []}, {"name": "RPTLINE", "fields": ["CUSNO", "CUSNAME", "CUSLIMIT"]}]


def test_coverage_counts_unmodelled_constructs():
    out = fixture("qddssrc/CUSTDSP.dspf")
    counts = constructs(out)
    assert counts[("ibmi:dds_conditioning", False)] == 1 and counts[("ibmi:dds_display_attribute", False)] == 1
    assert counts[("ibmi:dds_display_field", True)] == 7


def test_fixed_form_procedure_specs():
    src = "\n".join([
        cols((6, "H"), (7, "NOMAIN")),
        cols((6, "D"), (7, "GetTot"), (24, "PR"), (39, "9"), (40, "P"), (41, "2")),
        cols((6, "D"), (9, "code"), (39, "3"), (40, "A"), (44, "CONST")),
        cols((6, "P"), (7, "GetTot"), (24, "B"), (44, "EXPORT")),
        cols((6, "D"), (7, "GetTot"), (24, "PI"), (39, "9"), (40, "P"), (41, "2")),
        cols((6, "D"), (9, "code"), (39, "3"), (40, "A"), (44, "CONST")),
        c4(op="EXSR", f2="Calc"),
        c4(op="RETURN", ext="0"),
        c4(f1="Calc", op="BEGSR"),
        c4(op="ENDSR"),
        cols((6, "P"), (7, "GetTot"), (24, "E")),
    ]) + "\n"
    out = run(src, "x/TOTS.rpgle")
    ents, rels, us = entities(out), relations(out), units(out)
    got = ents[sym("TOTS", "GETTOT")]
    assert got["exported"] is True and got["returns"] == "packed(9:2)" and got["params"] == ["code"]
    assert (sym("*EXPORT", "GETTOT"), "ALIAS_OF", sym("TOTS", "GETTOT")) in rels
    assert (sym("TOTS", "GETTOT"), "CALLS", sym("TOTS", "GETTOT.CALC")) in rels
    assert us["GETTOT"]["kind"] == "function" and us["GETTOT.CALC"]["kind"] == "paragraph"
    assert "TOTS" not in us  # a NOMAIN module has no main procedure


# ------------------------------------------------------------------ review fixes (W8)


def screens_and_forms(rels) -> set[tuple]:
    return {r for r in rels if r[1] in ("RENDERS", "SUBMITS_TO")}


def test_a_format_of_one_of_two_display_files_is_not_guessed():
    src = "**FREE\ndcl-f ORDDSP workstn;\ndcl-f HLPDSP workstn include(HLPPNL);\nexfmt ORDPNL;\nread ORDPNL;\nexfmt HLPPNL;\n*inlr = *on;\n"
    out = run(src, "x/TWODSP.rpgle")
    rels = relations(out)
    # HLPPNL is HLPDSP's by its INCLUDE keyword; ORDPNL could be either file's.
    assert screens_and_forms(rels) == {
        (prog("TWODSP"), "RENDERS", key("Screen", platform="ibmi", name="HLPDSP.HLPPNL")),
        (key("Form", screen="HLPDSP.HLPPNL", name="HLPPNL"), "SUBMITS_TO", prog("TWODSP")),
    }
    assert constructs(out)[("ibmi:format_unresolved", False)] == 2
    notes = [d for d in out.diagnostics if "ORDPNL" in d.message]
    assert len(notes) == 2 and all(d.severity == "info" for d in notes)
    assert notes[0].message == "record format ORDPNL (EXFMT at line 4) may belong to any of HLPDSP, ORDDSP: no screen is linked"


def test_a_single_display_file_owns_formats_only_nothing_else_could():
    # EXFMT shows a display format: with one WORKSTN file it is that file's.
    one = run("**FREE\ndcl-f ORDDSP workstn;\ndcl-f ORDRPT printer;\nexfmt ORDPNL;\nwrite HEADING;\n", "x/ONEDSP.rpgle")
    rels = relations(one)
    assert (prog("ONEDSP"), "RENDERS", key("Screen", platform="ibmi", name="ORDDSP.ORDPNL")) in rels
    # WRITE may write the printer file's format as well: not linked to the display file.
    assert not any("HEADING" in r[2][1] for r in rels)
    assert constructs(one)[("ibmi:format_unresolved", False)] == 1
    alone = run("**FREE\ndcl-f ORDDSP workstn;\nwrite ORDHDR;\nread ORDPNL;\n", "x/ALONE.rpgle")
    rels = relations(alone)
    assert (prog("ALONE"), "RENDERS", key("Screen", platform="ibmi", name="ORDDSP.ORDHDR")) in rels
    assert (key("Form", screen="ORDDSP.ORDPNL", name="ORDPNL"), "SUBMITS_TO", prog("ALONE")) in rels
    assert ("ibmi:format_unresolved", False) not in constructs(alone)


def test_text_after_a_second_free_is_not_read():
    src = "**FREE\ndcl-f CUSTMAST disk;\nread CUSTMAST;\n*inlr = *on;\n**FREE\ndcl-f STRAY disk usage(*output);\nwrite STRAYREC;\n"
    out = run(src, "x/TWICE.rpgle")
    rels = relations(out)
    assert (prog("TWICE"), "READS_FROM", table("custmast")) in rels
    assert not any("stray" in r[2][1].lower() for r in rels)
    assert not any(f["name"] == "STRAY" for f in entities(out)[prog("TWICE")]["files"])
    assert "text from line 5, after a second **FREE, is not read: a member holds one program" in messages(out)
    assert constructs(out)[("ibmi:text_not_read", False)] == 1


def test_text_after_the_last_procedure_is_not_read():
    free = "**FREE\ndcl-f CUSTMAST disk;\nread CUSTMAST;\ndcl-proc Show;\n  dsply 'x';\nend-proc;\n\ndcl-f STRAY disk usage(*output);\nwrite STRAYREC;\nOther();\n"
    out = run(free, "x/AFTER.rpgle")
    rels = relations(out)
    assert (prog("AFTER"), "READS_FROM", table("custmast")) in rels and (prog("AFTER"), "CONTAINS", sym("AFTER", "SHOW")) in rels
    assert not any("stray" in r[2][1].lower() or "other" in r[2][1].lower() for r in rels)
    assert "text from line 8, after the last procedure, is not read: nothing but procedures may follow a procedure, so it belongs to another program" in messages(out)
    assert not any(m.startswith("other is called") for m in messages(out))
    assert constructs(out)[("ibmi:text_not_read", False)] == 1
    fixed = "\n".join([
        cols((6, "H"), (7, "NOMAIN")),
        cols((6, "P"), (7, "GetTot"), (24, "B"), (44, "EXPORT")),
        c4(op="RETURN", ext="0"),
        cols((6, "P"), (7, "GetTot"), (24, "E")),
        cols((6, "H"), (7, "DFTACTGRP(*NO)")),
        cols((6, "F"), (7, "STRAY"), (17, "O"), (18, "F"), (22, "E"), (36, "DISK")),
        c4(op="CALL", f2="'OTHER'"),
    ]) + "\n"
    out = run(fixed, "x/AFTERF.rpgle")
    rels = relations(out)
    assert (prog("AFTERF"), "CONTAINS", sym("AFTERF", "GETTOT")) in rels
    assert not any("stray" in r[2][1].lower() or "OTHER" in r[2][1] for r in rels)
    assert any(m.startswith("text from line 5, after the last procedure") for m in messages(out))
    # Procedures that follow each other, with directives between them, are all read.
    two = run("**FREE\nctl-opt nomain;\ndcl-proc A;\nend-proc;\n/copy qrpglesrc,protos\ndcl-proc B;\nend-proc;\n", "x/TWOPROC.rpgle")
    assert (prog("TWOPROC"), "CONTAINS", sym("TWOPROC", "B")) in relations(two)
    assert ("ibmi:text_not_read", False) not in constructs(two)


def test_cl_text_after_endpgm_is_not_read():
    src = "PGM\n  CALL PGM(FIRST)\nENDPGM\nPGM\n  CALL PGM(SECOND)\nENDPGM\n"
    out = run(src, "x/TWOCL.clp")
    rels = relations(out)
    assert (prog("TWOCL"), "CALLS", prog("FIRST")) in rels and (prog("TWOCL"), "CALLS", prog("SECOND")) not in rels
    assert "text from line 4, after ENDPGM, is not read: a member holds one program" in messages(out)
    assert constructs(out)[("ibmi:text_not_read", False)] == 1


def _compile_files(root: Path, files: dict[str, str]):
    import os
    import subprocess

    from repo_intel.compile.pipeline import compile_commit, load_shards
    from repo_intel.storage.store import ObjectStore

    repo = root / "repo"
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    store = ObjectStore(root / "store", "t")
    shards = load_shards(store, compile_commit(store, repo, "github.com/4040", sha).manifest)
    return {e["id"]: e for s in shards.values() for e in s["entities"]}, [a for s in shards.values() for a in s["assertions"]]


def test_dds_sql_ddl_and_embedded_sql_name_one_table(tmp_path):
    # Contract 11.3 item 1: names are lower case wherever they come from.
    ddl = "CREATE TABLE CUSTMAST (\n  CUSNO DECIMAL(7, 0) NOT NULL,\n  CUSNAME CHAR(30),\n  CUSLIMIT DECIMAL(9, 2),\n  CUSSTAT CHAR(1)\n);\n"
    ents, assertions = _compile_files(tmp_path, {
        "qddssrc/CUSTMAST.pf": (F40 / "qddssrc/CUSTMAST.pf").read_text(),
        "sql/custmast.sql": ddl,
        "qrpglesrc/CUSTUPD.sqlrpgle": (F40 / "qrpglesrc/CUSTUPD.sqlrpgle").read_text(),
    })
    tables = [e for e in ents.values() if e["kind"] == "Table" and e["natural_key"]["name"].endswith("custmast")]
    assert [t["natural_key"] for t in tables] == [{"datastore": "default", "name": "custmast"}]
    fields = sorted((e["natural_key"]["table"], e["natural_key"]["name"]) for e in ents.values() if e["kind"] == "Field")
    assert fields == [("custmast", n) for n in ("cuslimit", "cusname", "cusno", "cusstat")]
    tid = tables[0]["id"]
    contained = {ents[a["object"]]["natural_key"]["name"] for a in assertions if a["predicate"] == "CONTAINS" and a["subject"] == tid}
    assert contained == {"cuslimit", "cusname", "cusno", "cusstat"}
    program = next(e["id"] for e in ents.values() if e["kind"] == "Program" and e["natural_key"]["name"] == "CUSTUPD")
    assert {a["predicate"] for a in assertions if a["subject"] == program and a["object"] == tid} == {"READS_FROM", "WRITES_TO"}
