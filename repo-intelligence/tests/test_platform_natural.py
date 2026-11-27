"""The Natural and Adabas platform pack (repo_intel/platforms/natural): programs,
subprograms, subroutines, maps, data areas and DDMs (`natural.source`)."""

import hashlib
import json
import time
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import validate_unit
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.platforms import packs, validate_pack
from repo_intel.platforms.natural import PACK
from repo_intel.platforms.natural.lexer import prepare, tokens
from repo_intel.platforms.natural.source import NaturalSource

ROOT = Path(__file__).resolve().parents[1]
F42 = ROOT / "benchmarks" / "fixtures" / "F42-natural-adabas" / "base"
SOURCES = [
    "natural/EMPMAINT.nsp",
    "natural/EMPSAL.nsn",
    "natural/EMPLDA.nsl",
    "natural/EMPMAP.nsm",
    "natural/EMPLOYEES.nsd",
    "natural/VEHICLES.nsd",
    "natural/VEHRPT.nsp",
]


def run(text: str | bytes, path: str):
    data = text if isinstance(text, bytes) else text.encode()
    return NaturalSource().extract(path, data, hashlib.sha256(data).hexdigest())


def fixture(path: str):
    return run((F42 / path).read_bytes(), path)


def ref(x) -> tuple:
    return x if isinstance(x, str) else (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def key(entity_kind: str, **nk) -> tuple:
    return (entity_kind, json.dumps(nk, sort_keys=True))


def entities(out) -> dict[tuple, dict]:
    found: dict[tuple, dict] = {}
    for o in out.observations:
        if o.kind == "entity":
            found.setdefault(ref(o.payload), {}).update(o.payload["attributes"])
    return found


def relations(out) -> set[tuple]:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])) for o in out.observations if o.kind == "relation"}


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


def prog(name: str, platform: str = "natural") -> tuple:
    return key("Program", platform=platform, name=name)


def table(name: str, datastore: str = "adabas") -> tuple:
    return key("Table", datastore=datastore, name=name)


def walk(nodes):
    """Every statement node of a body, depth first."""
    for n in nodes:
        if not isinstance(n, dict):
            continue
        yield n
        for k in ("body", "then", "else", "default"):
            if isinstance(n.get(k), list):
                yield from walk(n[k])
        for c in n.get("cases", []) or []:
            yield from walk(c.get("body", []))


# ---------------------------------------------------------------------- the pack


def test_pack_is_registered_and_valid():
    assert packs()["natural"] is PACK
    assert PACK.prefix == "nat" and PACK.version == "1.1.0"
    assert validate_pack(PACK) == []
    assert {e.id for e in PACK.extractors} == {"natural.source"}
    assert len(PACK.constructs) >= 100


def test_extractor_claims_natural_suffixes():
    src = NaturalSource()
    for suffix in (".nsp", ".nsn", ".nss", ".nsh", ".nsm", ".nsl", ".nsa", ".nsg", ".nsc", ".nsd", ".NSP"):
        assert src.matches(f"lib/OBJ{suffix}"), suffix
    assert not src.matches("a.cbl") and not src.matches("a.ns") and not src.matches("a.nsx")


def test_the_pack_imports_extractors_only_inside_functions():
    import ast

    for p in (ROOT / "repo_intel" / "platforms" / "natural").glob("*.py"):
        tree = ast.parse(p.read_text())
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("repo_intel.extractors"), p.name
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("repo_intel.extractors") for a in node.names), p.name


def test_every_term_and_construct_the_code_names_is_listed():
    import re

    terms = {t.name for t in PACK.terms}
    ids = {c.id for c in PACK.constructs}
    source = "".join(p.read_text() for p in (ROOT / "repo_intel" / "platforms" / "natural").glob("*.py") if p.name != "vocabulary.py")
    used_terms = set(re.findall(r'"(nat:[A-Z][A-Za-z]*)"', source))
    used_constructs = set(re.findall(r'"(nat:[a-z][a-z_]*)"', source))
    assert used_terms and used_terms <= terms, sorted(used_terms - terms)
    assert used_constructs and used_constructs <= ids, sorted(used_constructs - ids)


@pytest.mark.parametrize("path", SOURCES)
def test_fixture_files_parse_into_valid_units_relations_and_known_constructs(path):
    out = fixture(path)
    assert out.parsed
    known = {c.id for c in PACK.constructs}
    assert {cid for cid, _ in constructs(out)} <= known
    for o in out.observations:
        if o.kind == "behavior":
            assert o.payload["language"] == "natural"
            assert validate_unit(o.payload) == [], path
        if o.kind == "relation" and isinstance(o.payload["subject"], dict) and isinstance(o.payload["object"], dict):
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p
        if o.kind == "entity" and o.payload["kind"] in ("Program", "Symbol", "Screen"):
            assert o.payload["natural_key"]["platform"] == "natural"


def test_output_is_deterministic():
    for path in SOURCES:
        a, b = fixture(path), fixture(path)
        assert [o.payload for o in a.observations] == [o.payload for o in b.observations]
        assert messages(a) == messages(b)


# ---------------------------------------------------------------------- lexing


def test_comments_and_editor_line_numbers_are_blanked_with_offsets_kept():
    raw = "0010 * a comment\n0020 WRITE 'x' /* trailing\n0030 END\n"
    text, mode, numbers = prepare(raw)
    assert len(text) == len(raw)
    assert "comment" not in text and "trailing" not in text and "0020" not in text
    assert mode is None and numbers == {10: 0, 20: 1, 30: 2}
    toks = tokens(text)
    assert [t.text for t in toks] == ["WRITE", "'x'", "END"]
    assert prepare("* :Mode R\nEND\n")[1] == "reporting"


def test_lexer_reads_labels_system_variables_and_qualified_names():
    from repo_intel.platforms.natural.lexer import LABEL, SYSVAR

    toks = tokens("R1. READ EMP BY NAME\n#X := *ISN(R1.) + EMP.SALARY(1)\n")
    assert toks[0].kind == LABEL and toks[0].text.rstrip(".") == "R1"
    assert any(t.kind == SYSVAR and t.text.upper() == "*ISN" for t in toks)
    assert any(t.text.upper() == "EMP.SALARY" for t in toks)


def test_lexing_and_parsing_are_linear_in_the_source():
    def program(n: int) -> str:
        body = "".join(f"  IF #A{i} > {i}\n    #B := #B + 1\n  END-IF\n" for i in range(n))
        return "DEFINE DATA LOCAL\n1 #B (N9)\nEND-DEFINE\nREAD EMP BY NAME\n" + body + "END-READ\nEND\n"

    def timed(n: int) -> float:
        best = 1e9
        for _ in range(2):
            t = time.perf_counter()
            run(program(n), "BIG.nsp")
            best = min(best, time.perf_counter() - t)
        return best

    small, large = timed(500), timed(4000)
    assert large < max(small, 0.01) * 20  # 8x the input: far less than quadratic


def test_deep_nesting_is_reported_not_fatal():
    text = "IF #A = 1\n" * 3000 + "WRITE 'x'\n" + "END-IF\n" * 3000 + "END\n"
    out = run(text, "DEEP.nsp")
    assert out.parsed
    assert key("Program", platform="natural", name="DEEP") in entities(out)
    assert any("nesting too deep" in m for m in messages(out)) or units(out)


# ---------------------------------------------------------------------- programs


def test_program_relations_from_the_fixture():
    out = fixture("natural/EMPMAINT.nsp")
    rel = relations(out)
    me = prog("EMPMAINT")
    assert (me, "DEPENDS_ON", key("DataSchema", kind="natural-lda", name="EMPLDA")) in rel
    assert (me, "RENDERS", key("Screen", platform="natural", name="EMPMAP")) in rel
    assert (me, "CALLS", prog("EMPSAL")) in rel
    assert (me, "CALLS", prog("AUDITLOG", "mainframe")) in rel
    assert (me, "READS_FROM", table("EMPLOYEES")) in rel
    assert (me, "WRITES_TO", table("EMPLOYEES")) in rel
    assert (me, "READS_FROM", table("VEHICLES")) in rel
    assert (me, "WRITES_TO", table("VEHICLES")) not in rel
    sub = key("Symbol", platform="natural", program="EMPMAINT", name="SHOW-TOTALS")
    assert (me, "CONTAINS", sub) in rel and (me, "CALLS", sub) in rel
    attrs = entities(out)[me]
    assert attrs["object_type"] == "program" and attrs["mode"] == "structured"
    assert attrs["views"] == {"EMP": "EMPLOYEES", "VEH": "VEHICLES"}
    calls = [o.payload for o in out.observations if o.kind == "relation" and o.payload["predicate"] == "CALLS"]
    assert all(c["object"].get("declare") is False for c in calls if c["object"]["kind"] == "Program")


def test_units_open_code_and_inline_subroutine():
    out = fixture("natural/EMPMAINT.nsp")
    u = units(out)
    assert u["EMPMAINT"]["kind"] == "open_code" and u["SHOW-TOTALS"]["kind"] == "function"
    ops = [n["op"] for n in walk(u["EMPMAINT"]["body"])]
    assert {"declare", "loop", "if", "assign", "call", "transaction", "break"} <= set(ops)
    loops = [n for n in walk(u["EMPMAINT"]["body"]) if n["op"] == "loop"]
    assert [lp["over"]["construct"] for lp in loops] == ["nat:read", "nat:find"]
    assert loops[0]["label"] == "RD1" and loops[0]["kind"] == "each"
    switch = next(n for n in walk(u["SHOW-TOTALS"]["body"]) if n["op"] == "switch")
    assert switch["cases"][1]["match"] == [{"e": "platform", "construct": "nat:value_range", "detail": {"from": {"e": "lit", "value": 1, "type": "integer"}, "to": {"e": "lit", "value": 9, "type": "integer"}}}]
    assert switch["default"]


def test_update_in_the_loop_that_read_the_record_is_a_projection_shape():
    out = fixture("natural/EMPMAINT.nsp")
    shapes = observations(out, "transform")
    assert len(shapes) == 1
    ops = [(t["op"], t.get("output"), t.get("columns")) for t in shapes[0]["transforms"]]
    assert ops == [("read", None, None), ("project", "employees", {"replace": ["salary"]})]


def test_dynamic_targets_stay_dynamic_and_are_counted():
    out = fixture("natural/EMPMAINT.nsp")
    assert constructs(out).get(("nat:fetch_return", False)) == 1
    assert any("through a variable" in m for m in messages(out))
    assert not any("#NEXT-PROG" in r[2][1] for r in relations(out))
    text = "CALLNAT #SUB #A\nFETCH 'MENU'\nINPUT USING MAP #M\nEND\n"
    out = run(text, "DYN.nsp")
    rel = relations(out)
    assert (prog("DYN"), "CALLS", prog("MENU")) in rel
    assert not any(p == "RENDERS" for _, p, _ in rel)
    assert constructs(out).get(("nat:callnat", False)) == 1
    assert sum("through a variable" in m for m in messages(out)) == 2


def test_subprogram_parameters_get_update_and_natural_sql():
    out = fixture("natural/EMPSAL.nsn")
    rel = relations(out)
    me = prog("EMPSAL")
    assert (me, "READS_FROM", table("EMPLOYEES")) in rel and (me, "WRITES_TO", table("EMPLOYEES")) in rel
    assert (me, "WRITES_TO", table("payroll_audit", "default")) in rel
    unit = units(out)["EMPSAL"]
    assert [p["name"] for p in unit["params"]] == ["#PERSONNEL-ID", "#WORK"]
    assert unit["params"][1]["type"] == "struct"
    query = next(n for n in walk(unit["body"]) if n["op"] == "query")
    assert query["host_vars"] == {"read": ["#PERSONNEL-ID"], "set": []}
    assert "#PERSONNEL-ID" not in query["sql"] and ":personnel_id" in query["sql"]


def test_reporting_mode_reads_the_ddm_by_its_own_name():
    out = fixture("natural/VEHRPT.nsp")
    me = prog("VEHRPT")
    assert entities(out)[me]["mode"] == "reporting"
    assert (me, "READS_FROM", table("VEHICLES")) in relations(out)
    c = constructs(out)
    assert c[("nat:do_doend", True)] == 1 and c[("nat:read", True)] == 1
    unit = units(out)["VEHRPT"]
    assert validate_unit(unit) == []
    loop = next(n for n in walk(unit["body"]) if n["op"] == "loop")
    assert any(n["op"] == "if" for n in walk(loop["body"]))


def test_reporting_mode_inline_definitions_are_counted_not_indexes():
    out = run("* :Mode R\nRESET #TOTAL (P9.2) #I (I2)\nADD 1 TO #I\nEND\n", "R.nsp")
    assert constructs(out)[("nat:inline_definition", False)] == 2
    body = units(out)["R"]["body"]
    assert body[0]["target"] == {"e": "var", "name": "#TOTAL"}


def test_a_view_defined_in_another_data_area_is_not_guessed():
    text = "DEFINE DATA LOCAL USING EMPLDA\nEND-DEFINE\nREAD EMP-V BY NAME\n  STORE EMP-V\nEND-READ\nEND\n"
    out = run(text, "UNRES.nsp")
    assert not any(p in ("READS_FROM", "WRITES_TO") for _, p, _ in relations(out))
    uses = observations(out, "natural_view_use")
    assert [(u["view"], u["statement"], u["data_areas"]) for u in uses] == [("EMP-V", "read", ["EMPLDA"]), ("EMP-V", "store", ["EMPLDA"])]
    summary = observations(out, "lineage_summary")[0]
    assert summary["unresolved"] == 2 and summary["resolved"] == 0
    assert sum("defined in a data area in another file" in m for m in messages(out)) == 1


def test_external_subroutine_is_named_by_its_define_subroutine():
    text = "DEFINE DATA PARAMETER USING CALCPDA\nEND-DEFINE\nDEFINE SUBROUTINE CALC-TAX\n  PERFORM OTHER-SUB\n  INCLUDE TAXCC\n  WRITE USING FORM 'TAXFORM' #X\nEND-SUBROUTINE\nEND\n"
    out = run(text, "lib/CALCSUB.nss")
    me = prog("CALC-TAX")
    attrs = entities(out)[me]
    assert attrs["object_type"] == "subroutine" and attrs["object_name"] == "CALCSUB"
    rel = relations(out)
    assert (me, "DEPENDS_ON", key("DataSchema", kind="natural-pda", name="CALCPDA")) in rel
    assert (me, "CALLS", prog("OTHER-SUB")) in rel
    assert (me, "DEPENDS_ON", prog("TAXCC")) in rel
    assert (me, "RENDERS", key("Screen", platform="natural", name="TAXFORM")) in rel
    assert units(out)["CALC-TAX"]["kind"] == "function"


def test_sql_statements_read_and_write_default_tables():
    text = (
        "DEFINE DATA LOCAL\n1 #ID (N8)\n1 #NAME (A20)\nEND-DEFINE\n"
        "SELECT NAME INTO #NAME FROM DSN8.EMP WHERE EMPNO = #ID\n  WRITE #NAME\nEND-SELECT\n"
        "UPDATE DSN8.EMP SET SALARY = SALARY * 1.1 WHERE EMPNO = :#ID\n"
        "DELETE FROM DSN8.TEMP WHERE EMPNO = #ID\nCOMMIT\nEND\n"
    )
    out = run(text, "SQLPGM.nsp")
    rel = relations(out)
    me = prog("SQLPGM")
    assert (me, "READS_FROM", table("dsn8.emp", "default")) in rel
    assert (me, "WRITES_TO", table("dsn8.emp", "default")) in rel
    assert (me, "WRITES_TO", table("dsn8.temp", "default")) in rel
    queries = [n for n in walk(units(out)["SQLPGM"]["body"]) if n["op"] == "query"]
    assert len(queries) == 3 and queries[0]["host_vars"] == {"read": ["#ID"], "set": ["#NAME"]}


# ---------------------------------------------------------------------- data areas, maps, DDMs


def test_data_area_fields_are_named_and_laid_out():
    out = fixture("natural/EMPLDA.nsl")
    ents = entities(out)
    lda = key("DataSchema", kind="natural-lda", name="EMPLDA")
    assert ents[lda]["object_type"] == "local_data_area"
    f = lambda name: ents[key("Field", table="natural:EMPLDA", name=name)]
    assert f("#WORK.#NAME")["offset"] == 0 and f("#WORK.#NAME")["parent"] == "#WORK"
    assert f("#PREVIOUS.#NAME")["parent"] == "#PREVIOUS"
    assert f("#ID")["offset"] == 20 and f("#ID")["length"] == 8
    assert f("#SALARY")["format"] == "P7.2" and f("#SALARY")["length"] == 5
    assert f("#CITIES")["occurs"] == 5
    assert key("Field", table="natural:EMPLDA", name="#NAME") not in ents
    rel = relations(out)
    assert (lda, "CONTAINS", key("Field", table="natural:EMPLDA", name="#ID")) in rel


def test_map_controls_named_by_field_with_positions_and_labels():
    out = fixture("natural/EMPMAP.nsm")
    ents = entities(out)
    screen = key("Screen", platform="natural", name="EMPMAP")
    assert ents[screen]["fields"] == 3
    pid = ents[key("UIControl", container="EMPMAP", name="#PERSONNEL-ID")]
    assert (pid["line"], pid["column"], pid["mode"], pid["label"], pid["helproutine"], pid["format"]) == (4, 16, "modifiable", "Personnel ID", "EMPHELP", "A8")
    assert ents[key("UIControl", container="EMPMAP", name="#MSG")]["mode"] == "output"
    assert (screen, "CONTAINS", key("UIControl", container="EMPMAP", name="#NAME")) in relations(out)


def test_ddm_fields_carry_short_names_formats_and_flags():
    out = fixture("natural/EMPLOYEES.nsd")
    ents = entities(out)
    t = table("EMPLOYEES")
    assert ents[t]["object_type"] == "ddm" and ents[t]["platform_kind"] == "nat:Ddm"
    f = lambda name: ents[key("Field", table="adabas:EMPLOYEES", name=name)]
    assert f("PERSONNEL-ID")["short_name"] == "AA" and f("PERSONNEL-ID")["descriptor"] == "unique"
    assert f("FULL-NAME")["group"] is True
    assert f("LANG")["mu"] is True
    assert f("INCOME")["pe"] is True and f("SALARY")["in_pe"] is True
    assert (f("SALARY")["format"], f("SALARY")["length"]) == ("P9", 5)
    assert (t, "CONTAINS", key("Field", table="adabas:EMPLOYEES", name="NAME")) in relations(out)
    text = (F42 / "natural/EMPLOYEES.nsd").read_text()
    assert "FILE: 11" in text and not any("11" == str(v) for a in ents.values() for v in a.values())


def test_a_sql_ddm_is_counted_not_declared():
    out = run("DDM NAME: DB2-EMP\nTYPE: DB2\n  1 AA NAME A 20\n", "DB2-EMP.nsd")
    assert constructs(out)[("nat:ddm_sql", False)] == 1
    assert not entities(out)


def test_names_never_values():
    data = (F42 / "natural/EMPMAINT.nsp").read_bytes()
    out = run(data, "natural/EMPMAINT.nsp")
    blob = json.dumps([o.payload for o in out.observations])
    assert "fixture-not-a-real-password-0042" not in blob
    assert constructs(out)[("nat:sensitive_value", True)] == 1
    out = run("DEFINE DATA LOCAL\n1 #HOST (A20) INIT <'db.example.internal'>\n1 #N (N3) CONST <42>\nEND-DEFINE\nMOVE 'svc-user-7' TO #USERID\nEND\n", "V.nsp")
    blob = json.dumps([o.payload for o in out.observations])
    assert "db.example.internal" not in blob and "svc-user-7" not in blob
    assert constructs(out)[("nat:init_value", True)] == 2


def test_literals_beside_a_credential_are_not_copied():
    # short values, which admission's secret detectors would not flag: the pack blanks them itself
    text = """DEFINE DATA LOCAL
1 U VIEW OF USERS
  2 API-TOKEN
  2 NAME
1 #PASSWORD (A20) INIT <'v01'>
1 #A (A20)
END-DEFINE
MOVE 'v02' TO #A #PASSWORD
IF #PASSWORD = 'v03'
  WRITE 'v04' #PASSWORD
END-IF
DECIDE ON FIRST VALUE OF #PASSWORD
  VALUE 'v05'
    IGNORE
  NONE
    IGNORE
END-DECIDE
DECIDE FOR FIRST CONDITION
  WHEN #PASSWORD = 'v06'
    IGNORE
  WHEN #A = 'kept-1'
    IGNORE
  WHEN NONE
    IGNORE
END-DECIDE
FIND U WITH API-TOKEN = 'v07'
  IGNORE
END-FIND
READ U BY API-TOKEN STARTING FROM 'v08'
  ACCEPT IF API-TOKEN = 'v09'
END-READ
EXAMINE #PASSWORD FOR 'v10' REPLACE 'v11'
CALLNAT 'SUBP' #PASSWORD 'v12'
REPEAT UNTIL #PASSWORD = 'v13'
  INPUT #PASSWORD
END-REPEAT
SELECT * INTO VIEW U FROM AUTH WHERE PASSWORD = 'v14'
END-SELECT
IF #A = 'kept-2'
  IGNORE
END-IF
END
"""
    out = run(text, "CRED.nsp")
    blob = json.dumps([o.payload for o in out.observations] + messages(out))
    for i in range(1, 15):
        assert f"'v{i:02d}'" not in blob and f'"v{i:02d}"' not in blob, i
    assert "kept-1" in blob and "kept-2" in blob  # literals of other fields stay
    for unit in units(out).values():
        assert validate_unit(unit) == []
    assert not any(o.payload["construct"] == "nat:for" for o in out.observations if o.kind == "construct")  # EXAMINE ... FOR is one statement
    assert constructs(out)[("nat:sensitive_value", True)] >= 12


def test_line_numbers_in_diagnostics_do_not_rescan_the_text():
    def program(n: int) -> str:
        return "DEFINE DATA LOCAL USING LDA1\nEND-DEFINE\n" + "".join(f"READ V{i} BY NAME\nEND-READ\n" for i in range(n)) + "END\n"

    def timed(n: int) -> float:
        text = program(n)
        best = 1e9
        for _ in range(2):
            t = time.perf_counter()
            out = run(text, "MANY.nsp")
            best = min(best, time.perf_counter() - t)
        assert f"view V{n - 1} at line {2 * n + 1} is defined in a data area in another file: its DDM is not read here" in messages(out)
        return best

    small, large = timed(2000), timed(8000)
    assert large < max(small, 0.01) * 8  # 4x the input: linear, with room for noise


def test_inventory_language_is_natural():
    from repo_intel.extractors import LANGUAGES

    for suffix in (".nsp", ".nsn", ".nss", ".nsm", ".nsl", ".nsh", ".nsc", ".nsa", ".nsg", ".nsd"):
        assert LANGUAGES[suffix] == "natural"


def test_a_redefinition_is_not_a_field_and_its_fields_say_what_they_redefine():
    out = run("DEFINE DATA PARAMETER\n1 #P1 (A10)\n1 #P2 (N3/1:4)\n1 REDEFINE #P2\n  2 #RAW (A12)\nEND-DEFINE\n", "PDA1.nsa")
    ents = entities(out)
    assert key("DataSchema", kind="natural-pda", name="PDA1") in ents
    raw = ents[key("Field", table="natural:PDA1", name="#RAW")]
    assert raw["redefines"] == "#P2" and raw["offset"] == 0 and "parent" not in raw
    assert not any(json.loads(k[1])["name"] == "REDEFINE" for k in ents if k[0] == "Field")
    assert constructs(out)[("nat:redefine", True)] == 1
