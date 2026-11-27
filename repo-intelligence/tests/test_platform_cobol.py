"""The COBOL platform pack (repo_intel/platforms/cobol): programs, copybooks, paragraphs,
files, embedded SQL and CICS (`cobol.source`), and its parity with `legacy.cobol`."""

import hashlib
import json
import re
import time
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import validate_unit
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.extractors import legacy
from repo_intel.platforms import packs, superseded, validate_pack
from repo_intel.platforms.cobol import PACK, CobolSource
from repo_intel.platforms.cobol import lexer

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "benchmarks" / "fixtures"
F14 = FIXTURES / "F14-mainframe-sas"
F37 = FIXTURES / "F37-cobol-batch-online"
F14_FILES = [
    "base/copybooks/CUSTREC.cpy",
    "base/cobol/DATEUTIL.cbl",
    "base/cobol/CUSTUPD.cbl",
    "commits/01-report-step/cobol/CUSTRPT.cbl",
    "commits/02-cics-inquiry/cobol/CUSTINQ.cbl",
]
F37_FILES = sorted(str(p.relative_to(F37)) for p in F37.rglob("*") if p.suffix.lower() in (".cbl", ".cpy"))


def run(text: str | bytes, path: str = "src/PROG.cbl"):
    data = text if isinstance(text, bytes) else text.encode()
    return CobolSource().extract(path, data, hashlib.sha256(data).hexdigest())


def fixed(*lines: str) -> str:
    """Lines of a fixed-format program: area A starts in column 8."""
    return "".join(" " * 7 + line + "\n" for line in lines)


def ref(x) -> tuple:
    return (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def key(kind_: str, **nk) -> tuple:
    return (kind_, json.dumps(nk, sort_keys=True))


def entities(out) -> dict[tuple, dict]:
    found: dict[tuple, dict] = {}
    for o in out.observations:
        if o.kind == "entity":
            found.setdefault(ref(o.payload), {}).update(o.payload["attributes"])
    return found


def relations(out) -> set[tuple]:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])) for o in out.observations if o.kind == "relation"}


def rows(out, predicate: str) -> list[dict]:
    return [o.payload for o in out.observations if o.kind == "relation" and o.payload["predicate"] == predicate]


def constructs(out) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = {}
    for o in out.observations:
        if o.kind == "construct":
            k = (o.payload["construct"], o.payload["modelled"])
            counts[k] = counts.get(k, 0) + 1
    return counts


def units(out) -> dict[str, dict]:
    return {o.payload["owner"]["name"]: o.payload for o in out.observations if o.kind == "behavior"}


def observations(out, kind: str) -> list[dict]:
    return [o.payload for o in out.observations if o.kind == kind]


def messages(out) -> list[str]:
    return [d.message for d in out.diagnostics]


def prog(name: str) -> tuple:
    return key("Program", platform="mainframe", name=name)


def sym(program: str, name: str) -> tuple:
    return key("Symbol", platform="mainframe", program=program, name=name)


def walk(nodes):
    for n in nodes:
        if not isinstance(n, dict):
            continue
        yield n
        for k in ("body", "then", "else", "default"):
            if isinstance(n.get(k), list):
                yield from walk(n[k])
        for c in (n.get("catches") or []) + (n.get("cases") or []):
            yield from walk(c.get("body", []))


def ops(unit: dict) -> list[str]:
    return [n["op"] for n in walk(unit["body"])]


# ---------------------------------------------------------------------- the pack


def test_pack_is_registered_valid_and_supersedes_legacy():
    assert packs()["cobol"] is PACK
    assert PACK.prefix == "cbl"
    assert validate_pack(PACK) == []
    assert {e.id for e in PACK.extractors} == {"cobol.source"}
    assert "legacy.cobol" in superseded()
    assert len(PACK.constructs) >= 100


def test_extractor_claims_cobol_suffixes():
    src = CobolSource()
    for path in ("a.cbl", "A.CBL", "a.cob", "a.cobol", "a.cpy"):
        assert src.matches(path), path
    assert not src.matches("a.jcl") and not src.matches("a.sas") and not src.matches("a.bms")


def test_the_pack_imports_extractors_only_inside_functions():
    import ast

    for p in (ROOT / "repo_intel" / "platforms" / "cobol").glob("*.py"):
        for node in ast.parse(p.read_text()).body:
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("repo_intel.extractors"), p.name
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("repo_intel.extractors") for a in node.names), p.name


def test_every_term_and_construct_the_code_names_is_listed():
    terms = {t.name for t in PACK.terms}
    ids = {c.id for c in PACK.constructs}
    source = "".join(p.read_text() for p in (ROOT / "repo_intel" / "platforms" / "cobol").glob("*.py") if p.name != "vocabulary.py")
    used_terms = set(re.findall(r'"(cbl:[A-Z][A-Za-z]*)"', source))
    used = set(re.findall(r'"(cbl:[a-z][a-z_]*[a-z])"', source))
    assert used_terms and used_terms <= terms, sorted(used_terms - terms)
    assert used and used <= ids, sorted(used - ids)
    # Ids built from a word at run time.
    families = {"cbl:" + v for v in ("add", "subtract", "multiply", "divide", "sort", "merge", "write", "rewrite", "section", "paragraph",
                                     "perform_until", "perform_times", "perform_varying", "report_section", "screen_section",
                                     "communication_section", "cics_link", "cics_xctl")}
    assert families <= ids, sorted(families - ids)


def test_every_listed_construct_is_honest_about_its_model():
    for c in PACK.constructs:
        assert c.description and not c.description.endswith(".")
        if c.id in ("cbl:alter", "cbl:procedure_copy", "cbl:sql_dynamic", "cbl:assign_dynamic", "cbl:replace"):
            assert c.modelled_as == "unmapped", c.id


# ---------------------------------------------------------------------- fixtures


@pytest.mark.parametrize("path", F14_FILES + F37_FILES)
def test_fixture_files_parse_into_valid_units_relations_and_known_constructs(path):
    base = F14 if path in F14_FILES else F37
    out = run((base / path).read_bytes(), path)
    assert out.parsed
    known = {c.id for c in PACK.constructs}
    assert {cid for cid, _ in constructs(out)} <= known
    for o in out.observations:
        if o.kind == "behavior":
            assert o.payload["language"] == "cobol" and o.payload["kind"] == "paragraph"
            assert validate_unit(o.payload) == [], path
        if o.kind == "relation":
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p
        if o.kind == "entity" and o.payload["kind"] in ("Program", "Symbol"):
            assert o.payload["natural_key"]["platform"] == "mainframe"
            assert o.payload["attributes"]["platform"] == "cobol"


@pytest.mark.parametrize("path", F14_FILES)
def test_emits_everything_legacy_cobol_emitted_with_the_same_kinds_and_keys(path):
    data = (F14 / path).read_bytes()
    old = legacy.Cobol().extract(path, data, "b")
    new = run(data, path)

    def keys(out):
        out_keys = set()
        for o in out.observations:
            p = o.payload
            if o.kind == "entity":
                out_keys.add(("entity", ref(p)))
            elif o.kind == "relation":
                out_keys.add(("relation", ref(p["subject"]), p["predicate"], ref(p["object"])))
        return out_keys

    assert keys(old) <= keys(new)
    old_attrs, new_attrs = entities(old), entities(new)
    for k, attrs in old_attrs.items():
        for name, value in attrs.items():
            if name == "platform":  # a pack's entities carry its id; the platform stays in the key
                assert new_attrs[k]["platform"] == "cobol"
                continue
            assert new_attrs[k].get(name) == value, (k, name)


def test_output_is_deterministic():
    for path in F37_FILES:
        a, b = run((F37 / path).read_bytes(), path), run((F37 / path).read_bytes(), path)
        assert [o.payload for o in a.observations] == [o.payload for o in b.observations]
        assert messages(a) == messages(b)


# ---------------------------------------------------------------------- lexing


def test_fixed_format_ignores_sequence_and_identification_areas_and_comments():
    text = (
        "000100 IDENTIFICATION DIVISION.                                         SEQ00001\n"
        "000200 PROGRAM-ID. SEQPROG.                                             SEQ00002\n"
        "000300*CALL 'NOTME'.\n"
        "000400/ NEW PAGE\n"
        "000500D    DISPLAY 'DEBUG'.\n"
        "000600 PROCEDURE DIVISION.\n"
        "000700     CALL 'REAL'.                                                 IGNORED1\n"
    )
    src = lexer.read(text)
    assert src.format == "fixed"
    words = [t.up for t in src.tokens]
    assert "NOTME" not in " ".join(words) and "SEQ00001" not in words and "IGNORED1" not in words and "DEBUG" not in " ".join(words)
    assert src.debug_lines == 1
    out = run(text)
    assert (prog("SEQPROG"), "CALLS", prog("REAL")) in relations(out)
    assert ("cbl:debug_line", False) in constructs(out)


def test_continued_literal_is_one_string():
    text = fixed(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. CONT.",
        "PROCEDURE DIVISION.",
        "    DISPLAY 'THIS LITERAL GOES ALL THE WAY TO COLUMN SEVENTY-TWO AN",
    ) + "      -    'D CONTINUES HERE'.\n"
    src = lexer.read(text)
    strings = [t for t in src.tokens if t.kind == lexer.STR]
    assert len(strings) == 1
    assert src.continuation_lines == 1


def test_free_format_by_directive_and_by_shape():
    free = ">>SOURCE FORMAT FREE\nIDENTIFICATION DIVISION.\nPROGRAM-ID. FREE1.\n*> a comment CALL 'NOTME'\nPROCEDURE DIVISION.\n    CALL 'TARGET'.\n"
    out = run(free)
    assert entities(out)[prog("FREE1")]["format"] == "free"
    assert (prog("FREE1"), "CALLS", prog("TARGET")) in relations(out)
    assert not any("NOTME" in json.dumps(o.payload) for o in out.observations)
    shaped = "IDENTIFICATION DIVISION.\nPROGRAM-ID. FREE2.\nPROCEDURE DIVISION.\nCALL 'T2'.\n"
    assert (prog("FREE2"), "CALLS", prog("T2")) in relations(run(shaped))


def test_picture_strings_are_one_token():
    src = lexer.read(fixed("01  A PIC -ZZZ,ZZ9.99.", "01  B PIC S9(7)V99 COMP-3."))
    pics = [t.text for t in src.tokens if t.kind == lexer.PIC]
    assert pics == ["-ZZZ,ZZ9.99", "S9(7)V99"]


def test_lexing_is_linear_on_long_hostile_input():
    def timed(n):
        body = fixed("IDENTIFICATION DIVISION.", "PROGRAM-ID. BIG.", "PROCEDURE DIVISION.", "P1.") + fixed(*(["    MOVE 'A''B' TO X(1:2) ADD 1 TO Y."] * n))
        start = time.perf_counter()
        run(body)
        return time.perf_counter() - start

    small, large = timed(500), timed(4000)
    assert large < small * 8 * 3 + 0.5  # eight times the input, well under quadratic time


def test_diagnostics_and_the_one_literal_rule_are_linear():
    # Each dynamic CALL looks up its item and writes a diagnostic with a line number: neither
    # may scan the file or every data item again for each call.
    def timed(n):
        items = [f"01  WS-P{k} PIC X(8)." for k in range(n)]
        calls = [f"    CALL WS-P{k} PERFORM P{k} THRU P{k}" for k in range(n)]
        paras = [f"P{k}. CONTINUE." for k in range(n)]
        body = fixed("IDENTIFICATION DIVISION.", "PROGRAM-ID. BIG.", "DATA DIVISION.", "WORKING-STORAGE SECTION.", *items,
                     "PROCEDURE DIVISION.", "P-MAIN.", *calls, "    GOBACK.", *paras)
        start = time.perf_counter()
        out = run(body)
        assert sum("stays dynamic" in m for m in messages(out)) == n
        return time.perf_counter() - start

    small, large = timed(300), timed(2400)
    assert large < small * 8 * 3 + 0.5  # eight times the input, well under quadratic time


def test_unterminated_literal_and_binary_input_do_not_fail():
    assert run(fixed("IDENTIFICATION DIVISION.", "PROGRAM-ID. BAD.", "PROCEDURE DIVISION.", "    DISPLAY 'NEVER ENDS")).parsed
    out = run(b"\xff\xfe\x00\x01" * 50)
    assert out is not None


# ---------------------------------------------------------------------- structure


def test_paragraphs_and_sections_are_symbols_the_program_contains():
    out = run((F37 / "base/cobol/ACCTPOST.cbl").read_bytes(), "cobol/ACCTPOST.cbl")
    ents, rels = entities(out), relations(out)
    for name in ("0000-MAIN", "1000-INIT", "2000-READ-TXN", "2000-EXIT", "3000-POST", "9000-FINISH"):
        assert ents[sym("ACCTPOST", name)]["platform_kind"] == "cbl:Paragraph"
        assert (prog("ACCTPOST"), "CONTAINS", sym("ACCTPOST", name)) in rels
    assert not any(k[0] == "Symbol" and "TXN-EOF" in k[1] for k in ents)


def test_perform_thru_and_go_to_are_calls_between_procedures():
    out = run((F37 / "base/cobol/ACCTPOST.cbl").read_bytes(), "cobol/ACCTPOST.cbl")
    via = {(r["subject"]["name"], r["object"]["name"]): r["qualifiers"].get("via") for r in rows(out, "CALLS") if r["object"]["kind"] == "Symbol"}
    assert via[("0000-MAIN", "1000-INIT")] == "perform"
    assert via[("0000-MAIN", "2000-READ-TXN")] == "perform_thru"
    assert via[("0000-MAIN", "2000-EXIT")] == "perform_thru"
    assert via[("2000-READ-TXN", "2000-EXIT")] == "go_to"
    assert via[("2000-READ-TXN", "3000-POST")] == "perform"


def test_sections_qualify_repeated_paragraph_names_and_resolve_locally():
    out = run(fixed(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. SECTS.",
        "PROCEDURE DIVISION.",
        "A-SECTION SECTION.",
        "START-IT.",
        "    PERFORM WORK-IT.",
        "WORK-IT.",
        "    EXIT.",
        "B-SECTION SECTION.",
        "START-IT.",
        "    PERFORM WORK-IT.",
        "    PERFORM A-SECTION.",
        "WORK-IT.",
        "    EXIT.",
    ))
    ents, rels = entities(out), relations(out)
    assert ents[sym("SECTS", "A-SECTION")]["platform_kind"] == "cbl:Section"
    for s in ("A-SECTION", "B-SECTION"):
        assert (sym("SECTS", f"{s}.START-IT"), "CALLS", sym("SECTS", f"{s}.WORK-IT")) in rels
    assert (sym("SECTS", "B-SECTION.START-IT"), "CALLS", sym("SECTS", "A-SECTION")) in rels


def test_statements_before_the_first_paragraph_are_a_unit_of_the_program():
    out = run((F14 / "base/cobol/CUSTUPD.cbl").read_bytes(), "cobol/CUSTUPD.cbl")
    unit = units(out)["CUSTUPD"]
    assert unit["owner"]["kind"] == "Program" and unit["kind"] == "paragraph"
    assert [n["op"] for n in unit["body"]] == ["call", "platform", "query", "query", "call", "return"]


def test_nested_programs_are_each_programs():
    out = run(fixed(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. OUTER.",
        "PROCEDURE DIVISION.",
        "    CALL 'INNER'.",
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. INNER.",
        "PROCEDURE DIVISION.",
        "    CALL 'DEEP'.",
        "END PROGRAM INNER.",
        "END PROGRAM OUTER.",
    ))
    ents, rels = entities(out), relations(out)
    assert ents[prog("INNER")]["nested_in"] == "OUTER"
    assert (prog("OUTER"), "CALLS", prog("INNER")) in rels
    assert (prog("INNER"), "CALLS", prog("DEEP")) in rels
    assert (prog("OUTER"), "CALLS", prog("DEEP")) not in rels


def test_a_file_with_no_program_id_is_parsed_and_says_so():
    out = run(fixed("PROCEDURE DIVISION.", "    DISPLAY 'X'."), "src/ODD.cbl")
    assert out.parsed and "no PROGRAM-ID" in messages(out)


# ---------------------------------------------------------------------- behaviour


def test_paragraph_units_lower_the_statements():
    out = run((F37 / "commits/01-fee-rule/cobol/ACCTPOST.cbl").read_bytes(), "cobol/ACCTPOST.cbl")
    u = units(out)
    post = u["3000-POST"]
    assert post["owner"]["kind"] == "Symbol"
    switch = post["body"][0]
    assert switch["op"] == "switch" and switch["cases"][0]["match"][0]["condition_name"] is True
    assert "if" in ops(post) and "call" in ops(post) and "output" in ops(post)
    fee = u["3100-APPLY-FEE"]
    assert fee["body"][0]["op"] == "if" and fee["body"][1]["op"] == "try"  # ON SIZE ERROR
    main = u["0000-MAIN"]
    loop = main["body"][1]
    assert loop["op"] == "loop" and loop["kind"] == "until" and loop["body"][0]["thru"] == "2000-EXIT"
    assert loop["cond"]["condition_name"] == "TXN-EOF" and loop["cond"]["right"]["value"] == "10"


def test_file_io_conditions_are_ifs_on_a_platform_condition():
    out = run((F37 / "base/cobol/ACCTPOST.cbl").read_bytes(), "cobol/ACCTPOST.cbl")
    read = units(out)["2000-READ-TXN"]
    conds = [n["cond"]["detail"]["condition"] for n in walk(read["body"]) if n["op"] == "if" and n["cond"].get("e") == "platform"]
    assert conds == ["at_end", "invalid_key"]


def test_embedded_sql_is_a_query_with_host_variables():
    out = run((F37 / "base/cobol/ACCTINQ.cbl").read_bytes(), "cobol/ACCTINQ.cbl")
    read, q = units(out)["1000-LOOKUP"]["body"][:2]  # the CICS READ of the master, then the SELECT
    assert read["op"] == "platform" and q["op"] == "query"
    assert q["sql"] == "SELECT ACCT_NAME, BALANCE FROM ACCOUNTS WHERE ACCT_ID = :WS_ACCT_ID"
    assert q["host_vars"] == {"read": ["WS-ACCT-ID"], "set": ["WS-ACCT-NAME", "WS-BALANCE"]}
    assert [i["name"] for i in q["target"]["items"]] == ["WS-ACCT-NAME", "WS-BALANCE"]


def test_a_cursor_opened_runs_its_declared_query():
    out = run((F37 / "commits/02-statement-program/cobol/STMTGEN.cbl").read_bytes(), "cobol/STMTGEN.cbl")
    first = units(out)["LOAD-ROWS"]["body"][0]
    assert first["op"] == "query" and first["cursor"] == "ACCT-CSR" and "FROM ACCOUNTS" in first["sql"]
    rels = relations(out)
    assert (prog("STMTGEN"), "READS_FROM", key("Table", datastore="default", name="accounts")) in rels
    assert (prog("STMTGEN"), "WRITES_TO", key("Table", datastore="default", name="statement_runs")) in rels
    assert ("cbl:sql_declare_cursor", True) in constructs(out)


def test_unlowered_statements_are_counted_not_dropped():
    out = run(fixed(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. ODDS.",
        "PROCEDURE DIVISION.",
        "P1.",
        "    ALTER P2 TO PROCEED TO P3",
        "    XML GENERATE OUT-DOC FROM REC",
        "    GO TO P2.",
        "P2.",
        "    GO TO P3.",
        "P3.",
        "    STOP RUN.",
    ))
    counts = constructs(out)
    assert counts.get(("cbl:alter", False)) and counts.get(("cbl:xml", False))
    assert validate_unit(units(out)["P1"]) == []


# ---------------------------------------------------------------------- calls


def test_a_call_through_an_item_with_one_literal_value_resolves():
    out = run((F37 / "base/cobol/ACCTPOST.cbl").read_bytes(), "cobol/ACCTPOST.cbl")
    calls = {r["object"]["name"]: r["qualifiers"] for r in rows(out, "CALLS") if r["object"]["kind"] == "Program"}
    assert calls == {"FMTAMT": {"resolved_from": "WS-FMT-PGM"}}
    node = next(n for n in walk(units(out)["3000-POST"]["body"]) if n["op"] == "call" and n["call"]["callee"].get("resolved_from"))
    assert node["call"]["callee"] == {"e": "name", "name": "FMTAMT", "kind": "program", "resolved_from": "WS-FMT-PGM"}
    assert constructs(out)[("cbl:call_dynamic", True)] == 1


def test_a_call_through_an_item_with_two_values_stays_dynamic_and_is_counted():
    out = run((F37 / "base/cobol/ACCTPOST.cbl").read_bytes(), "cobol/ACCTPOST.cbl")
    ents = entities(out)
    assert prog("LOANRULE") not in ents and prog("DEPRULE") not in ents
    assert not any(r["object"]["name"] in ("LOANRULE", "DEPRULE", "WS-RULE-PGM") for r in rows(out, "CALLS"))
    assert ents[prog("ACCTPOST")]["dynamic_calls"] == 1
    assert constructs(out)[("cbl:call_dynamic", False)] == 1
    assert observations(out, "lineage_summary")[0]["unresolved"] == 1
    assert any("WS-RULE-PGM" in m and "dynamic" in m for m in messages(out))


@pytest.mark.parametrize("data_division,procedure", [
    ("01  PGM PIC X(8) VALUE 'ONE'.", "    MOVE WS-OTHER TO PGM\n"),  # a non-literal write
    ("01  GRP.\n           05  PGM PIC X(8) VALUE 'ONE'.", "    MOVE SPACES TO GRP\n"),  # a write to the group holding it
    ("01  PGM PIC X(8) VALUE 'ONE'.\n       01  ALT REDEFINES PGM PIC X(8).", ""),  # redefined
    ("01  PGM PIC X(8) OCCURS 2.", ""),  # a table
])
def test_the_one_literal_rule_refuses_to_guess(data_division, procedure):
    text = fixed("IDENTIFICATION DIVISION.", "PROGRAM-ID. GUESS.", "DATA DIVISION.", "WORKING-STORAGE SECTION.") + "       " + data_division + "\n" + fixed("PROCEDURE DIVISION.") + ("       " + procedure if procedure else "") + fixed("    CALL PGM.")
    out = run(text)
    assert not any(r["object"]["kind"] == "Program" for r in rows(out, "CALLS"))
    assert entities(out)[prog("GUESS")]["dynamic_calls"] == 1


def test_a_linkage_item_is_never_resolved():
    out = run(fixed(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. LINKED.", "DATA DIVISION.", "LINKAGE SECTION.", "01  LK-PGM PIC X(8) VALUE 'NOPE'.",
        "PROCEDURE DIVISION USING LK-PGM.", "    CALL LK-PGM.",
    ))
    assert not rows(out, "CALLS")


def test_dli_and_mq_calls_are_constructs_with_attributes():
    out = run(fixed(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. IMSMQ.",
        "DATA DIVISION.",
        "WORKING-STORAGE SECTION.",
        "01  GU-FUNC   PIC X(4) VALUE 'GU  '.",
        "01  SSA-CUST  PIC X(9) VALUE 'CUSTOMER '.",
        "01  MQOD-OBJECTNAME PIC X(48) VALUE 'ORDERS.IN'.",
        "LINKAGE SECTION.",
        "01  PCB-1 PIC X(40).",
        "PROCEDURE DIVISION.",
        "    CALL 'CBLTDLI' USING GU-FUNC PCB-1 IO-AREA SSA-CUST",
        "    CALL 'MQOPEN' USING HCONN MQOD OPTS HOBJ CC RC",
        "    CALL 'MQPUT' USING HCONN HOBJ MD PMO LEN BUF CC RC.",
    ))
    attrs = entities(out)[prog("IMSMQ")]
    assert attrs["dli_segments"] == ["CUSTOMER"] and attrs["dli_functions"] == ["GU"]
    assert attrs["mq_calls"] == ["MQOPEN", "MQPUT"] and attrs["mq_queues"] == ["ORDERS.IN"]
    counts = constructs(out)
    assert counts[("cbl:dli_call", True)] == 1 and counts[("cbl:mq_call", True)] == 2
    # legacy.cobol linked the interface stubs as programs; they still are, marked as such.
    assert {r["object"]["name"]: r["qualifiers"]["interface"] for r in rows(out, "CALLS")} == {"CBLTDLI": "dli", "MQOPEN": "mq", "MQPUT": "mq"}


# ---------------------------------------------------------------------- copybooks


def test_copy_is_depends_on_and_replacing_is_recorded_not_expanded():
    out = run(fixed(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. COPIER.",
        "DATA DIVISION.",
        "WORKING-STORAGE SECTION.",
        "    COPY CUSTREC REPLACING ==:PFX:== BY ==WS==.",
        "    COPY CUSTREC.",
        "PROCEDURE DIVISION.",
        "    DISPLAY 'X'.",
    ))
    deps = rows(out, "DEPENDS_ON")
    assert [d["object"]["natural_key"] for d in deps] == [{"kind": "copybook", "name": "CUSTREC"}]
    assert entities(out)[prog("COPIER")]["copy_replacing"] == [{"book": "CUSTREC", "replacing": [["==:PFX:==", "==WS=="]]}]
    assert constructs(out)[("cbl:copy_replacing", True)] == 1


def test_copybook_fields_layout_and_condition_names():
    out = run((F37 / "base/copybooks/ACCTREC.cpy").read_bytes(), "copybooks/ACCTREC.cpy")
    ents, rels = entities(out), relations(out)
    book = key("DataSchema", kind="copybook", name="ACCTREC")
    status = key("Field", table="cobol:ACCTREC", name="ACCT-STATUS")
    assert ents[book]["platform_kind"] == "cbl:Copybook"
    assert (book, "CONTAINS", status) in rels
    assert ents[key("Field", table="cobol:ACCTREC", name="ACCT-BALANCE")].items() >= {"pic": "S9(11)V99", "usage": "COMP-3", "offset": 43, "length": 7}.items()
    assert ents[key("Field", table="cobol:ACCTREC", name="ACCT-REC")]["length"] == 83
    codes = key("CodeList", platform="cobol", name="ACCTREC.ACCT-STATUS")
    assert (status, "BINDS_TO", codes) in rels
    assert ents[codes]["values"] == [{"code": "A", "label": "ACCT-ACTIVE"}, {"code": "F", "label": "ACCT-FROZEN"}, {"code": "C", "label": "ACCT-CLOSED"}]
    risk = ents[key("CodeList", platform="cobol", name="ACCTREC.ACCT-RISK-SCORE")]["values"]
    assert risk[0] == {"code": "0", "label": "ACCT-LOW-RISK", "through": "299"}
    assert not any(k[0] == "Field" and "FILLER" in k[1] for k in ents)


def test_layout_of_usages_redefines_and_occurs():
    out = run(fixed(
        "01  REC.",
        "    05  A  PIC S9(4) COMP.",
        "    05  B  PIC S9(7)V99 COMP-3.",
        "    05  C  PIC X(3) OCCURS 4.",
        "    05  D  REDEFINES C PIC X(12).",
        "    05  E  COMP-2.",
        "    05  F  PIC 9(5).",
    ), "copybooks/LAYOUT.cpy")
    got = {k: (v.get("offset"), v.get("length")) for k, v in entities(out).items() if k[0] == "Field"}
    f = lambda n: got[key("Field", table="cobol:LAYOUT", name=n)]
    assert f("A") == (0, 2) and f("B") == (2, 5) and f("C") == (7, 3) and f("D") == (7, 12) and f("E") == (19, 8) and f("F") == (27, 5)
    assert f("REC") == (0, 32)


# ---------------------------------------------------------------------- files


def test_select_fd_and_open_modes_are_dd_files_and_attributes():
    out = run((F37 / "base/cobol/ACCTPOST.cbl").read_bytes(), "cobol/ACCTPOST.cbl")
    dd = {d["dd"]: d for d in observations(out, "dd_file")}
    assert set(dd) == {"TXNIN", "ACCTMST", "RPTOUT"}  # UT-S-TXNIN is DD TXNIN
    assert dd["TXNIN"]["mode"] == "input" and dd["ACCTMST"]["mode"] == "i-o" and dd["RPTOUT"]["mode"] == "output"
    assert dd["RPTOUT"]["record"] == "REPORT-LINE" and dd["TXNIN"]["select"] == "TXN-FILE"
    assert dd["TXNIN"]["program"]["natural_key"] == {"platform": "mainframe", "name": "ACCTPOST"}
    files = {f["select"]: f for f in entities(out)[prog("ACCTPOST")]["files"]}
    assert files["ACCT-MASTER"]["organization"] == "indexed" and files["ACCT-MASTER"]["access"] == "random"
    assert files["ACCT-MASTER"]["record_key"] == "ACCT-ID" and files["ACCT-MASTER"]["copybooks"] == ["ACCTREC"]
    assert files["TXN-FILE"]["record_length"] == 33


def test_a_sort_file_and_a_dynamic_assign_are_not_dd_files():
    out = run(fixed(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. SORTS.",
        "ENVIRONMENT DIVISION.",
        "INPUT-OUTPUT SECTION.",
        "FILE-CONTROL.",
        "    SELECT IN-F ASSIGN TO INDD.",
        "    SELECT OUT-F ASSIGN TO OUTDD.",
        "    SELECT WORK-F ASSIGN TO SORTWK.",
        "    SELECT DYN-F ASSIGN USING WS-NAME.",
        "DATA DIVISION.",
        "FILE SECTION.",
        "FD  IN-F.",
        "01  IN-R PIC X(10).",
        "FD  OUT-F.",
        "01  OUT-R PIC X(10).",
        "SD  WORK-F.",
        "01  WORK-R.",
        "    05 K PIC X(4).",
        "FD  DYN-F.",
        "01  DYN-R PIC X(10).",
        "PROCEDURE DIVISION.",
        "    SORT WORK-F ON DESCENDING KEY K USING IN-F GIVING OUT-F.",
    ))
    assert {d["dd"] for d in observations(out, "dd_file")} == {"INDD", "OUTDD"}
    assert ("cbl:assign_dynamic", False) in constructs(out)
    shapes = observations(out, "transform")
    sort = shapes[0]["transforms"][-1]
    assert (sort["op"], sort["inputs"], sort["order"], sort["output"]) == ("sort", ["in-f"], [{"column": "k", "desc": True}], "out-f")


# ---------------------------------------------------------------------- CICS


def test_cics_link_xctl_maps_and_transactions():
    out = run(fixed(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. ONLINE1.",
        "DATA DIVISION.",
        "WORKING-STORAGE SECTION.",
        "01  WS-NEXT PIC X(8) VALUE 'MENU01'.",
        "01  WS-ANY  PIC X(8).",
        "PROCEDURE DIVISION.",
        "    EXEC CICS LINK PROGRAM('SUBPGM') COMMAREA(CA) END-EXEC",
        "    EXEC CICS SEND MAP('MAP1') MAPSET('SET1') ERASE END-EXEC",
        "    EXEC CICS SEND MAP('ALONE') END-EXEC",
        "    EXEC CICS RECEIVE MAP('MAP1') MAPSET('SET1') END-EXEC",
        "    EXEC CICS START TRANSID('TR02') END-EXEC",
        "    EXEC CICS XCTL PROGRAM(WS-NEXT) END-EXEC",
        "    ACCEPT WS-ANY",
        "    EXEC CICS XCTL PROGRAM(WS-ANY) END-EXEC",
        "    EXEC CICS RETURN TRANSID('TR01') END-EXEC.",
    ))
    rels = relations(out)
    me = prog("ONLINE1")
    assert (me, "CALLS", prog("SUBPGM")) in rels and (me, "CALLS", prog("MENU01")) in rels
    assert (me, "RENDERS", key("Screen", platform="bms", name="SET1.MAP1")) in rels
    assert (me, "RENDERS", key("Screen", platform="bms", name="ALONE.ALONE")) in rels
    tran = lambda t: key("Endpoint", component_root="@unit", protocol="cics", method="TRANSACTION", route=t)
    assert (me, "CONSUMES", tran("TR01")) in rels and (me, "CONSUMES", tran("TR02")) in rels
    assert all(r["object"].get("declare") is False for r in rows(out, "RENDERS") + rows(out, "CONSUMES"))
    maps = entities(out)[me]["maps"]
    assert {"map": "MAP1", "mapset": "SET1", "op": "receive"} in maps and {"map": "MAP1", "mapset": "SET1", "op": "send"} in maps
    counts = constructs(out)
    assert counts[("cbl:cics_xctl", True)] == 1 and counts[("cbl:cics_xctl", False)] == 1
    assert entities(out)[me]["dynamic_calls"] == 1


def test_cics_file_commands_keep_legacy_datasets():
    out = run((F14 / "commits/02-cics-inquiry/cobol/CUSTINQ.cbl").read_bytes(), "cobol/CUSTINQ.cbl")
    ds = key("Dataset", name="custmast")
    assert entities(out)[ds] == {"access": "cics_file"}
    assert (prog("CUSTINQ"), "READS_FROM", ds) in relations(out)


def test_other_cics_commands_are_counted():
    out = run(fixed(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. ONLINE2.", "PROCEDURE DIVISION.",
        "    EXEC CICS ASKTIME ABSTIME(WS-T) END-EXEC",
        "    EXEC CICS WRITEQ TS QUEUE('Q1') FROM(X) END-EXEC",
        "    EXEC CICS SYNCPOINT END-EXEC.",
    ))
    counts = constructs(out)
    assert counts[("cbl:cics_time", False)] == 1 and counts[("cbl:cics_queue", False)] == 1 and counts[("cbl:cics_syncpoint", True)] == 1


# ---------------------------------------------------------------------- names, never values


def test_names_never_values():
    out = run((F37 / "base/cobol/ACCTPOST.cbl").read_bytes(), "cobol/ACCTPOST.cbl")
    dumped = json.dumps([o.payload for o in out.observations]) + json.dumps(messages(out))
    assert "fixture-not-a-real-password-0037" not in dumped
    moved = next(n for n in walk(units(out)["1000-INIT"]["body"]) if n.get("target", {}).get("name") == "WS-DB-PASSWD")
    assert moved["redacted"] == "credential" and moved["value"]["value"] == ""
    # A VALUE clause is never copied, except an 88-level's codes in a copybook and the
    # one literal a CALL resolves to.
    out = run(fixed(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. HIDDEN.", "DATA DIVISION.", "WORKING-STORAGE SECTION.",
        "01  WS-HOST PIC X(30) VALUE 'db.internal.example'.", "01  WS-USER PIC X(8) VALUE 'BATCHUSR'.",
        "PROCEDURE DIVISION.", "    EXEC SQL CONNECT TO :WS-HOST USER :WS-USER USING :WS-PW END-EXEC.",
    ))
    dumped = json.dumps([o.payload for o in out.observations])
    assert "db.internal.example" not in dumped and "BATCHUSR" not in dumped


def test_condition_names_of_a_credential_are_never_copied():
    # A short value (under eight characters) is not one a secret scanner flags, so the
    # pack itself must leave it out.
    out = run(fixed(
        "01  SEC-REC.",
        "    05  SEC-USER        PIC X(8).",
        "    05  SEC-PASSWD      PIC X(6).",
        "        88  SEC-PASSWD-DEFAULT  VALUE 'Zq7!x'.",
        "        88  SEC-PASSWD-BLANK    VALUE SPACES.",
        "    05  SEC-MODE        PIC X.",
        "        88  SEC-MODE-ADMIN      VALUE 'A'.",
        "    05  SEC-AREA.",
        "        10  SEC-PIN-TOKEN PIC 9(4).",
        "            88  SEC-PIN-FIXED   VALUE 4711.",
    ), "copybooks/SECREC.cpy")
    ents, rels, counts = entities(out), relations(out), constructs(out)
    pw = key("Field", table="cobol:SECREC", name="SEC-PASSWD")
    codes = key("CodeList", platform="cobol", name="SECREC.SEC-PASSWD")
    assert (pw, "BINDS_TO", codes) in rels
    assert "values" not in ents[codes] and ents[codes]["redacted"] is True
    pin = ents[key("CodeList", platform="cobol", name="SECREC.SEC-PIN-TOKEN")]
    assert "values" not in pin and pin["redacted"] is True
    mode = ents[key("CodeList", platform="cobol", name="SECREC.SEC-MODE")]
    assert mode["values"] == [{"code": "A", "label": "SEC-MODE-ADMIN"}] and "redacted" not in mode
    assert counts[("cbl:condition_name", False)] == 3 and counts[("cbl:condition_name", True)] == 1
    dumped = json.dumps([o.payload for o in out.observations]) + json.dumps(messages(out))
    assert "Zq7!x" not in dumped

    # In a program: a test, a SET and an EVALUATE of such an 88 name it without its value;
    # an 88 itself named like a credential is treated the same way.
    out = run(fixed(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. LOGON.", "DATA DIVISION.", "WORKING-STORAGE SECTION.",
        "01  WS-PWD             PIC X(5).",
        "    88  WS-PWD-OK      VALUE 'Zq7!x'.",
        "01  WS-IN              PIC X(3).",
        "    88  WS-IN-SECRET   VALUE 'k9#'.",
        "PROCEDURE DIVISION.",
        "MAIN-PARA.",
        "    IF WS-PWD-OK",
        "        DISPLAY 'IN'",
        "    END-IF",
        "    SET WS-PWD-OK TO TRUE",
        "    EVALUATE TRUE",
        "        WHEN WS-IN-SECRET CONTINUE",
        "    END-EVALUATE",
        "    GOBACK.",
    ))
    unit = units(out)["MAIN-PARA"]
    validate_unit(unit)
    dumped = json.dumps([o.payload for o in out.observations]) + json.dumps(messages(out))
    assert "Zq7!x" not in dumped and "k9#" not in dumped
    tested = next(n for n in walk(unit["body"]) if n["op"] == "if")
    assert tested["cond"] == {"e": "var", "name": "WS-PWD-OK", "condition_name": True}
    assert any(n["op"] == "platform" and n.get("construct") == "cbl:set_condition" for n in walk(unit["body"]))


def test_when_values_compared_with_a_credential_are_never_copied():
    # An EVALUATE's WHEN values are what its subject is compared with: blanked when the
    # subject or the WHEN itself names a credential, kept otherwise.
    out = run(fixed(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. LOGON.", "DATA DIVISION.", "WORKING-STORAGE SECTION.",
        "01  WS-PWD             PIC X(5).",
        "01  WS-X               PIC X(5).",
        "PROCEDURE DIVISION.",
        "MAIN-PARA.",
        "    EVALUATE WS-PWD",
        "        WHEN 'Rt5y' CONTINUE",
        "        WHEN OTHER CONTINUE",
        "    END-EVALUATE",
        "    EVALUATE TRUE",
        "        WHEN WS-PWD = 'Rt5z' CONTINUE",
        "        WHEN WS-X = 'kept1' CONTINUE",
        "    END-EVALUATE",
        "    EVALUATE WS-X ALSO WS-PWD",
        "        WHEN 'a' ALSO 'Rt5w' CONTINUE",
        "    END-EVALUATE",
        "    EVALUATE WS-X",
        "        WHEN 'kept2' CONTINUE",
        "    END-EVALUATE",
        "    GOBACK.",
    ))
    unit = units(out)["MAIN-PARA"]
    validate_unit(unit)
    dumped = json.dumps([o.payload for o in out.observations]) + json.dumps(messages(out))
    for value in ("Rt5y", "Rt5z", "Rt5w"):
        assert value not in dumped, value
    assert "kept1" in dumped and "kept2" in dumped
    switches = [n for n in unit["body"] if n["op"] == "switch"]
    assert [s.get("redacted") for s in switches] == ["credential", "credential", "credential", None]
