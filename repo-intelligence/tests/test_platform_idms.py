"""The IDMS platform pack (repo_intel/platforms/idms): schema and subschema source
(`idms.source`)."""

import hashlib
import json
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.platforms import packs, validate_pack
from repo_intel.platforms.idms import PACK
from repo_intel.platforms.idms import ddl
from repo_intel.platforms.idms.source import IdmsSource

ROOT = Path(__file__).resolve().parents[1]
IDMS = ROOT / "benchmarks" / "fixtures" / "F42-natural-adabas" / "base" / ".rkp" / "inputs" / "idms"
SCHEMA = ".rkp/inputs/idms/empschm.schema"
SUBSCHEMA = ".rkp/inputs/idms/empss01.txt"


def run(text: str | bytes, path: str):
    data = text if isinstance(text, bytes) else text.encode()
    return IdmsSource().extract(path, data, hashlib.sha256(data).hexdigest())


def fixture(path: str):
    return run((IDMS / Path(path).name).read_bytes(), path)


def ref(x) -> tuple:
    return (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


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


def messages(out) -> list[str]:
    return [d.message for d in out.diagnostics]


def record(name: str, schema: str = "EMPSCHM") -> tuple:
    return key("Table", datastore=f"idms:{schema}", name=name)


def element(rec: str, name: str, schema: str = "EMPSCHM") -> tuple:
    return key("Field", table=f"idms:{schema}.{rec}", name=name)


# ---------------------------------------------------------------------- the pack


def test_pack_is_registered_and_valid():
    assert packs()["idms"] is PACK
    assert PACK.prefix == "idms" and PACK.version == "1.1.0"
    assert validate_pack(PACK) == []
    assert {e.id for e in PACK.extractors} == {"idms.source"}
    assert PACK.inputs == (".rkp/inputs/idms/**",)


def test_files_are_claimed_by_path_or_suffix_and_cobol_is_never_read():
    src = IdmsSource()
    assert src.matches(".rkp/inputs/idms/anything.txt") and src.matches("sub/.rkp/inputs/idms/x")
    for path in ("db/EMP.schema", "db/EMP.sch", "db/EMP.idms", "db/EMPSS.subschema", "db/x.ddl"):
        assert src.matches(path), path
    assert not src.matches(".rkp/inputs/idms/PAYROLL.cbl") and not src.matches("src/PAYROLL.cbl")
    assert not src.matches("db/create.sql")


def test_the_pack_imports_extractors_only_inside_functions():
    import ast

    for p in (ROOT / "repo_intel" / "platforms" / "idms").glob("*.py"):
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
    source = "".join(p.read_text() for p in (ROOT / "repo_intel" / "platforms" / "idms").glob("*.py") if p.name != "vocabulary.py")
    used_terms = set(re.findall(r'"(idms:[A-Z][A-Za-z]*)"', source))
    used_constructs = set(re.findall(r'"(idms:[a-z][a-z_]*)"', source))
    assert used_terms == terms
    assert used_constructs == ids, sorted(used_constructs ^ ids)


@pytest.mark.parametrize("path", [SCHEMA, SUBSCHEMA])
def test_fixture_files_give_valid_relations_and_known_constructs(path):
    out = fixture(path)
    assert out.parsed
    assert {cid for cid, _ in constructs(out)} <= {c.id for c in PACK.constructs}
    for o in out.observations:
        if o.kind == "relation":
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p


def test_output_is_deterministic():
    for path in (SCHEMA, SUBSCHEMA):
        a, b = fixture(path), fixture(path)
        assert [o.payload for o in a.observations] == [o.payload for o in b.observations]
        assert messages(a) == messages(b)


# ---------------------------------------------------------------------- schemas


def test_records_are_tables_keyed_by_schema_with_location_and_sets():
    out = fixture(SCHEMA)
    ents = entities(out)
    schema = key("DataSchema", kind="idms-schema", name="EMPSCHM")
    assert ents[schema]["areas"] == ["EMP-REGION"] and ents[schema]["sets"] == ["DEPT-EMPLOYEE"]
    dept, emp = ents[record("DEPARTMENT")], ents[record("EMPLOYEE")]
    assert (dept["record_id"], dept["location_mode"], dept["calc_key"], dept["duplicates"], dept["area"]) == (410, "calc", ["DEPT-ID"], "not allowed", "EMP-REGION")
    assert dept["sets"] == [{"set": "DEPT-EMPLOYEE", "member": "EMPLOYEE", "order": "sorted"}]
    assert (emp["location_mode"], emp["via_set"]) == ("via", "DEPT-EMPLOYEE")
    assert emp["member_of"] == [{"set": "DEPT-EMPLOYEE", "owner": "DEPARTMENT", "membership": "mandatory automatic", "key": ["LAST-NAME"], "key_order": "ascending"}]
    assert "sets" not in emp
    assert dept["platform_kind"] == "idms:Record" and dept["fidelity"] == "lossy"
    rel = relations(out)
    assert (schema, "CONTAINS", record("DEPARTMENT")) in rel and (schema, "CONTAINS", record("EMPLOYEE")) in rel


def test_elements_have_pictures_usages_offsets_and_lengths():
    out = fixture(SCHEMA)
    ents = entities(out)
    assert ents[element("DEPARTMENT", "DEPT-NAME")]["offset"] == 4 and ents[element("DEPARTMENT", "DEPT-NAME")]["length"] == 45
    name = ents[element("EMPLOYEE", "EMP-NAME")]
    assert name["group"] is True and name["length"] == 25
    last = ents[element("EMPLOYEE", "LAST-NAME")]
    assert (last["level"], last["parent"], last["offset"], last["length"], last["pic"]) == (3, "EMP-NAME", 14, 15, "X(15)")
    sal = ents[element("EMPLOYEE", "SALARY")]
    assert (sal["pic"], sal["usage"], sal["length"], sal["offset"]) == ("S9(7)V99", "COMP-3", 5, 31)
    assert ents[record("DEPARTMENT")]["length"] == 52 and ents[record("EMPLOYEE")]["length"] == 36
    assert element("DEPARTMENT", "FILLER") not in ents
    assert constructs(out)[("idms:element", False)] == 1
    assert (record("EMPLOYEE"), "CONTAINS", element("EMPLOYEE", "SALARY")) in relations(out)


def test_88_levels_are_a_code_list_the_field_binds_to():
    out = fixture(SCHEMA)
    codes = key("CodeList", platform="idms", name="EMPSCHM.EMPLOYEE.STATUS-CODE")
    attrs = entities(out)[codes]
    assert attrs["values"] == ["01", "02", "03"] and attrs["conditions"] == ["ACTIVE", "RETIRED"]
    assert (element("EMPLOYEE", "STATUS-CODE"), "BINDS_TO", codes) in relations(out)


def test_subschema_contains_its_records_and_named_elements_without_declaring_them():
    out = fixture(SUBSCHEMA)
    ents = entities(out)
    sub = key("DataSchema", kind="idms-subschema", name="EMPSS01")
    assert ents[sub]["schema"] == "EMPSCHM" and ents[sub]["records"] == ["DEPARTMENT", "EMPLOYEE"] and ents[sub]["areas"] == ["EMP-REGION"]
    assert list(ents) == [sub]
    rel = relations(out)
    assert (sub, "CONTAINS", record("EMPLOYEE")) in rel and (sub, "CONTAINS", record("DEPARTMENT")) in rel
    assert (sub, "CONTAINS", element("EMPLOYEE", "EMP-NAME")) in rel
    objects = [o.payload["object"] for o in out.observations if o.kind == "relation"]
    assert all(o.get("declare") is False for o in objects)


def test_the_older_schema_ddl_reads_the_same_way():
    text = """SCHEMA DESCRIPTION.
SCHEMA NAME IS OLDSCH.
AREA NAME IS MAIN-AREA.
RECORD NAME IS CUSTOMER.
RECORD ID IS 100.
LOCATION MODE IS CALC USING CUST-ID DUPLICATES ARE NOT ALLOWED.
WITHIN MAIN-AREA.
02 CUST-ID PIC X(8).
02 CUST-BAL PIC S9(5)V99 COMP-3.
RECORD NAME IS ORDER-REC.
LOCATION MODE IS VIA CUST-ORDER SET.
02 ORDER-NO PIC 9(6).
SET NAME IS CUST-ORDER.
ORDER IS LAST.
MODE IS CHAIN.
OWNER IS CUSTOMER.
MEMBER IS ORDER-REC OPTIONAL MANUAL.
"""
    out = run(text, "db/old.sch")
    ents = entities(out)
    cust = ents[record("CUSTOMER", "OLDSCH")]
    assert (cust["record_id"], cust["location_mode"], cust["area"], cust["length"]) == (100, "calc", "MAIN-AREA", 12)
    assert cust["sets"] == [{"set": "CUST-ORDER", "member": "ORDER-REC", "order": "last"}]
    assert ents[record("ORDER-REC", "OLDSCH")]["member_of"][0]["membership"] == "optional manual"
    assert element("CUSTOMER", "CUST-BAL", "OLDSCH") in ents


def test_records_with_no_schema_are_counted_not_declared():
    out = run("ADD SCHEMA NAME IS X.\n", "a.schema")
    assert key("DataSchema", kind="idms-schema", name="X") in entities(out)
    text = "ADD SUBSCHEMA NAME IS S1.\nADD RECORD R1.\n"
    out = run(text, "a.ssc")
    assert any("names no schema" in m for m in messages(out))
    # a schema file cut before its SCHEMA statement: records with no datastore
    out = run("* SCHEMA NAME IS lost\nADD SCHEMA DESCRIPTION.\nADD RECORD NAME IS R2.\n    02 F1 PIC X.\n", "b.schema")
    assert not any(k[0] == "Table" for k in entities(out))
    assert constructs(out)[("idms:record", False)] == 1


def test_other_files_are_left_alone():
    out = run("CREATE TABLE T (A INT);\n", "db/t.ddl")
    assert out.parsed is False and out.observations == []
    out = run("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. X.\n", ".rkp/inputs/idms/readme.txt")
    assert out.parsed is False


def test_physical_statements_and_directives_are_counted():
    text = "ADD SCHEMA NAME IS P.\nADD FILE EMPFILE ASSIGN TO EMPDD.\nADD SEGMENT SEG1.\nVALIDATE.\nGENERATE.\n"
    c = constructs(run(text, "p.schema"))
    assert c[("idms:physical", False)] == 2 and c[("idms:directive", False)] == 2


def test_values_are_never_copied_except_condition_values():
    text = "ADD SCHEMA NAME IS V.\nADD RECORD NAME IS R.\n    02 PW PIC X(20) VALUE 'fixture-secret-value-1'.\n    02 ST PIC X.\n       88 OK VALUE 'Y'.\n"
    out = run(text, "v.schema")
    blob = json.dumps([o.payload for o in out.observations])
    assert "fixture-secret-value-1" not in blob
    assert entities(out)[element("R", "PW", "V")]["initialized"] is True


def test_a_credentials_condition_values_are_not_copied():
    # short values, which admission's secret detectors would not flag: the pack must blank them itself
    text = ("ADD SCHEMA NAME IS V.\nADD RECORD NAME IS R.\n    02 DB-PASSWORD PIC X(4).\n       88 PW-DEFAULT VALUE 'k9z'.\n"
            "    02 AUTH.\n       03 API-TOKEN-CODE PIC X(3).\n          88 TOKEN-TEST VALUE 'q7x' 'w2y'.\n"
            "    02 ST PIC X.\n       88 SECRET-MODE VALUE 'm4v'.\n    02 KIND PIC X.\n       88 OK VALUE 'Y'.\n")
    out = run(text, "v.schema")
    blob = json.dumps([o.payload for o in out.observations])
    for value in ("k9z", "q7x", "w2y", "m4v"):
        assert value not in blob
    ents = entities(out)
    for field, conditions in (("DB-PASSWORD", ["PW-DEFAULT"]), ("API-TOKEN-CODE", ["TOKEN-TEST"]), ("ST", ["SECRET-MODE"])):
        attrs = ents[key("CodeList", platform="idms", name=f"V.R.{field}")]
        assert attrs["redacted"] is True and "values" not in attrs and attrs["conditions"] == conditions
        assert (element("R", field, "V"), "BINDS_TO", key("CodeList", platform="idms", name=f"V.R.{field}")) in relations(out)
    assert ents[key("CodeList", platform="idms", name="V.R.KIND")]["values"] == ["Y"]
    assert "redacted" not in ents[key("CodeList", platform="idms", name="V.R.KIND")]


def test_many_areas_and_occurs_clauses_are_read_in_linear_time():
    import time

    def timed(make, n: int) -> float:
        text = make(n)
        best = 1e9
        for _ in range(2):
            t = time.perf_counter()
            run(text, "big.schema")
            best = min(best, time.perf_counter() - t)
        return best

    def areas(n: int) -> str:
        return "ADD SCHEMA NAME IS S.\n" + "".join(f"ADD AREA NAME IS AREA-{i}.\n" for i in range(n))

    def occurs(n: int) -> str:
        return "ADD SCHEMA NAME IS S.\nADD RECORD NAME IS R.\n    02 A" + " OCCURS 2" * n + " PIC X.\n"

    for make in (areas, occurs):
        small, large = timed(make, 2000), timed(make, 8000)
        assert large < max(small, 0.01) * 8  # 4x the input: linear, with room for noise


def test_picture_lengths():
    assert ddl.pic_length("X(10)", None) == 10
    assert ddl.pic_length("S9(7)V99", "COMP-3") == 5
    assert ddl.pic_length("9(4)", "BINARY") == 2
    assert ddl.pic_length("9(9)", "COMP") == 4
    assert ddl.pic_length("9(18)", "COMP") == 8
    assert ddl.pic_length("ZZ,ZZ9.99-", None) == 10
    assert ddl.pic_length(None, "COMP-2") == 8
    assert ddl.pic_length("X(", None) is None


def test_statement_splitting_is_linear():
    import time

    def timed(n: int) -> float:
        text = "ADD SCHEMA NAME IS BIG.\nADD RECORD NAME IS R.\n" + "".join(f"    02 F{i} PIC X(4).\n" for i in range(n))
        t = time.perf_counter()
        run(text, "big.schema")
        return time.perf_counter() - t

    small, large = timed(500), timed(4000)
    assert large < max(small, 0.01) * 20
