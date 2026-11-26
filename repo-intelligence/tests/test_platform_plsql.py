"""The Oracle PL/SQL platform pack (repo_intel/platforms/plsql): packages, procedures,
functions, triggers and SQL*Plus install scripts (`plsql.source`)."""

import hashlib
import json
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import validate_unit
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.platforms import packs, validate_pack
from repo_intel.platforms.plsql import PACK, PlsqlSource
from repo_intel.platforms.plsql.lexer import tokens
from repo_intel.platforms.plsql.source import recognise

FIXTURES = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures"
F28 = FIXTURES / "F28-oracle-plsql" / "base"
SOURCES = [
    "db/packages/order_api.pks",
    "db/packages/order_api.pkb",
    "db/procedures/purge_stale_carts.prc",
    "db/functions/customer_tier.fnc",
    "db/triggers/trg_orders_status.trg",
    "db/install/grants_and_jobs.sql",
]


def run(text: str | bytes, path: str):
    data = text if isinstance(text, bytes) else text.encode()
    return PlsqlSource().extract(path, data, hashlib.sha256(data).hexdigest())


def fixture(path: str):
    return run((F28 / path).read_bytes(), path)


def ref(x) -> tuple:
    return x if isinstance(x, str) else (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def key(kind: str, **nk) -> tuple:
    return (kind, json.dumps(nk, sort_keys=True))


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


def prog(name: str) -> tuple:
    return key("Program", platform="oracle", name=name)


def sym(program: str, name: str) -> tuple:
    return key("Symbol", platform="oracle", program=program, name=name)


def table(name: str, datastore: str = "default") -> tuple:
    return key("Table", datastore=datastore, name=name)


def role(name: str) -> tuple:
    return key("Role", realm="oracle", name=name)


def perm(name: str) -> tuple:
    return key("Permission", realm="oracle", name=name)


def walk(nodes):
    """Every statement node of a body, depth first."""
    for n in nodes:
        if not isinstance(n, dict):
            continue
        yield n
        for k in ("body", "then", "else"):
            if isinstance(n.get(k), list):
                yield from walk(n[k])
        for c in n.get("catches", []) or []:
            yield from walk(c.get("body", []))
        for c in n.get("cases", []) or []:
            yield from walk(c.get("body", []))


# ---------------------------------------------------------------------- the pack


def test_pack_is_registered_and_valid():
    assert packs()["plsql"] is PACK
    assert PACK.prefix == "ora"
    assert validate_pack(PACK) == []
    assert {e.id for e in PACK.extractors} == {"plsql.source"}
    assert len(PACK.constructs) >= 60


def test_extractor_claims_oracle_suffixes():
    src = PlsqlSource()
    for path in ("a.pks", "a.pkb", "a.pls", "a.plb", "a.prc", "a.fnc", "a.trg", "install.sql"):
        assert src.matches(path), path
    assert not src.matches("a.py") and not src.matches("a.jcl")


def test_the_pack_imports_extractors_only_inside_functions():
    import ast

    for p in (Path(__file__).resolve().parents[1] / "repo_intel" / "platforms" / "plsql").glob("*.py"):
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
    source = "".join(p.read_text() for p in (Path(__file__).resolve().parents[1] / "repo_intel" / "platforms" / "plsql").glob("*.py") if p.name != "vocabulary.py")
    used_terms = set(re.findall(r'"(ora:[A-Z][A-Za-z]*)"', source))
    used_constructs = set(re.findall(r'"(ora:[a-z][a-z_]*)"', source))
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
            assert o.payload["language"] == "plsql"
            assert validate_unit(o.payload) == [], path
        if o.kind == "relation" and isinstance(o.payload["subject"], dict) and isinstance(o.payload["object"], dict):
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p
        if o.kind == "entity" and o.payload["kind"] in ("Program", "Symbol"):
            assert o.payload["natural_key"]["platform"] == "oracle"
            assert o.payload["attributes"]["platform"] == "plsql"


def test_output_is_deterministic():
    for path in SOURCES:
        a, b = fixture(path), fixture(path)
        assert [o.payload for o in a.observations] == [o.payload for o in b.observations]
        assert messages(a) == messages(b)


def test_lexer_reads_quoted_strings_comments_and_slash_lines():
    toks = tokens("x := q'[it's]'; -- note\n/* block */ y := :new.a;\n/\n")
    kinds = [(t.kind, t.text) for t in toks]
    assert ("string", "q'[it's]'") in kinds
    assert not any("note" in t.text or "block" in t.text for t in toks)
    assert ("bind", ":new") in kinds
    assert toks[-1].kind == "slash"


# ---------------------------------------------------------------------- recognition


def test_a_jcl_prc_is_never_taken():
    data = (F28 / "jcl/NIGHTLY.prc").read_bytes()
    assert not recognise("jcl/NIGHTLY.prc", data.decode())
    out = run(data, "jcl/NIGHTLY.prc")
    assert out.parsed is False and out.observations == []


def test_an_oracle_prc_is_recognised_by_its_content():
    text = (F28 / "db/procedures/purge_stale_carts.prc").read_text()
    assert recognise("db/procedures/purge_stale_carts.prc", text)
    assert not recognise("x.prc", "-- nothing here\n")


@pytest.mark.parametrize("text", [
    "CREATE PROCEDURE dbo.GetOrders @CustomerId INT\nAS\nBEGIN\n  SET NOCOUNT ON;\n  SELECT * FROM dbo.Orders;\nEND\nGO\n",
    "CREATE OR REPLACE FUNCTION f(p integer) RETURNS integer AS $$\nBEGIN\n  RETURN 1;\nEND;\n$$ LANGUAGE plpgsql;\n",
    "SELECT * FROM orders;\nINSERT INTO t SELECT * FROM s;\n",
    "CREATE TRIGGER t BEFORE INSERT ON x FOR EACH ROW EXECUTE FUNCTION f();\n",
])
def test_other_sql_files_are_left_to_the_sql_extractors(text):
    assert not recognise("db/x.sql", text)
    assert run(text, "db/x.sql").parsed is False


def test_an_oracle_procedure_in_a_sql_file_is_recognised():
    text = "CREATE OR REPLACE PROCEDURE p AS\nBEGIN\n  DELETE FROM t;\nEND p;\n/\n"
    assert recognise("db/p.sql", text)
    out = run(text, "db/p.sql")
    assert prog("p") in entities(out) and (prog("p"), "WRITES_TO", table("t")) in relations(out)


# ---------------------------------------------------------------------- packages and units


def test_package_spec_declares_the_program_and_its_public_members():
    out = fixture("db/packages/order_api.pks")
    found = entities(out)
    assert found[prog("order_api")]["kind"] == "package"
    assert found[prog("order_api")]["specification"] == "db/packages/order_api.pks"
    assert found[prog("order_api")]["options"] == ["authid definer"]
    for name in ("place_order", "order_total", "close_order", "load_remote_customers", "archive_orders"):
        assert found[sym("order_api", name)]["public"] is True
        assert (prog("order_api"), "CONTAINS", sym("order_api", name)) in relations(out)
    assert found[sym("order_api", "order_total")]["symbol_kind"] == "function"
    # package variables and exceptions are seen, not modelled, in a spec
    assert constructs(out)[("ora:exception_declaration", False)] == 1
    assert constructs(out)[("ora:constant", False)] == 1


def test_package_body_members_contain_symbols_with_behaviour_and_lineage():
    out = fixture("db/packages/order_api.pkb")
    found, rels = entities(out), relations(out)
    assert found[prog("order_api")]["body"] == "db/packages/order_api.pkb"
    assert found[sym("order_api", "log_event")]["autonomous_transaction"] is True
    assert "public" not in found[sym("order_api", "log_event")]  # a body-only member
    po = sym("order_api", "place_order")
    assert {(po, "READS_FROM", table("customers")), (po, "READS_FROM", table("items")), (po, "WRITES_TO", table("orders")), (po, "WRITES_TO", table("order_lines"))} <= rels
    assert (po, "CALLS", sym("order_api", "log_event")) in rels
    # a package function called inside SQL is a call
    assert (sym("order_api", "close_order"), "CALLS", sym("order_api", "order_total")) in rels
    assert set(units(out)) == {"log_event", "place_order", "order_total", "close_order", "load_remote_customers", "archive_orders"}
    for unit in units(out).values():
        assert unit["kind"] == "function" and validate_unit(unit) == []


def test_body_with_an_exception_section_is_a_try_spanning_the_whole_block():
    out = fixture("db/packages/order_api.pkb")
    text = (F28 / "db/packages/order_api.pkb").read_bytes()
    body = units(out)["place_order"]["body"]
    tries = [n for n in body if n["op"] == "try"]
    assert len(tries) == 1
    a, b = tries[0]["at"]
    assert text[a:b].startswith(b"BEGIN") and text[a:b].rstrip().endswith(b"'Unknown customer or item');")
    assert tries[0]["catches"][0]["type"] == "no_data_found"
    archive = units(out)["archive_orders"]["body"]
    catch = [n for n in archive if n["op"] == "try"][0]["catches"][0]
    assert catch["type"] == "others" and catch["body"][-1]["op"] == "throw"


def test_select_into_keeps_the_query_and_names_its_target():
    out = fixture("db/functions/customer_tier.fnc")
    q = next(n for n in walk(units(out)["customer_tier"]["body"]) if n["op"] == "query")
    assert "INTO" not in q["sql"].upper().split("FROM")[0]
    assert q["target"]["name"] == "v_spend"
    assert q["params"] == [{"name": "p_customer_id", "value": {"e": "var", "scope": "arguments", "name": "p_customer_id"}}]
    found = entities(out)
    assert found[prog("customer_tier")]["returns"] == "VARCHAR2" and found[prog("customer_tier")]["params"] == ["p_customer_id"]


def test_cursor_for_loop_runs_the_cursor_query_then_loops_over_its_rows():
    out = fixture("db/packages/order_api.pkb")
    body = units(out)["order_total"]["body"]
    ops = [n["op"] for n in body]
    i = ops.index("query")
    assert ops[i + 1] == "loop" and body[i + 1]["kind"] == "each"
    assert "order_lines" in body[i]["sql"]
    assert (sym("order_api", "order_total"), "READS_FROM", table("order_lines")) in relations(out)
    assert constructs(out)[("ora:cursor_for_loop", True)] == 1


def test_merge_bulk_collect_and_forall():
    out = fixture("db/packages/order_api.pkb")
    counts = constructs(out)
    assert counts[("ora:merge", True)] == 1
    assert counts[("ora:bulk_collect", True)] == 1
    assert sum(n for (c, _), n in counts.items() if c == "ora:forall") == 2
    lrc = sym("order_api", "load_remote_customers")
    assert (lrc, "WRITES_TO", table("customers")) in relations(out)
    assert (lrc, "READS_FROM", table("crm_customers")) in relations(out)
    forall = [n for n in walk(units(out)["archive_orders"]["body"]) if n["op"] == "platform" and n["construct"] == "ora:forall"]
    assert len(forall) == 2 and all(n["body"] for n in forall)


def test_dynamic_sql_stays_dynamic_and_is_counted():
    out = fixture("db/packages/order_api.pkb")
    body = units(out)["archive_orders"]["body"]
    dynamic = [n for n in walk(body) if n["op"] == "query" and n.get("dynamic")]
    assert len(dynamic) == 2
    for q in dynamic:
        assert ":v_table" in q["sql"]
        assert any(p["name"] == "v_table" and p.get("inline") for p in q["params"])
    assert constructs(out)[("ora:execute_immediate", False)] == 2
    assert not any("orders_archive_" in k[1] for k in entities(out))
    assert sum("built at run time" in m for m in messages(out)) == 2


def test_dbms_sql_is_counted_and_a_literal_execute_immediate_is_read():
    text = """CREATE OR REPLACE PROCEDURE reset_rows(p_name IN VARCHAR2) AS
  v_cur INTEGER;
BEGIN
  v_cur := DBMS_SQL.OPEN_CURSOR;
  DBMS_SQL.PARSE(v_cur, 'DELETE FROM ' || p_name, DBMS_SQL.NATIVE);
  DBMS_SQL.CLOSE_CURSOR(v_cur);
  EXECUTE IMMEDIATE 'TRUNCATE TABLE scratch_rows';
END;
/
"""
    out = run(text, "db/reset_rows.prc")
    counts = constructs(out)
    assert counts[("ora:dbms_sql", False)] >= 1
    assert counts[("ora:execute_immediate", True)] == 1
    assert (prog("reset_rows"), "WRITES_TO", table("scratch_rows")) in relations(out)
    assert validate_unit(units(out)["reset_rows"]) == []


# ---------------------------------------------------------------------- triggers


def test_row_trigger_with_when_clause():
    out = fixture("db/triggers/trg_orders_status.trg")
    attrs = entities(out)[prog("trg_orders_status")]
    assert attrs["kind"] == "trigger" and attrs["level"] == "row"
    assert attrs["on"] == {"table": "orders", "events": ["update"], "timing": "before", "columns": ["status"]}
    unit = units(out)["trg_orders_status"]
    assert unit["kind"] == "trigger" and validate_unit(unit) == []
    guard = unit["body"][0]
    assert guard["op"] == "if" and guard["cond"]["left"] == {"e": "var", "scope": "new", "name": "status", "at": guard["cond"]["left"]["at"]}
    assign = next(n for n in walk(unit["body"]) if n["op"] == "assign")
    assert assign["target"]["scope"] == "new" and assign["target"]["name"] == "updated_at"
    q = next(n for n in walk(unit["body"]) if n["op"] == "query")
    assert {p["name"] for p in q["params"]} == {"new.order_id", "old.status", "new.status"}
    assert (prog("trg_orders_status"), "WRITES_TO", table("order_status_history")) in relations(out)


def test_referencing_names_keep_their_placeholders():
    text = (FIXTURES / "F28-oracle-plsql" / "commits" / "02-line-audit-trigger" / "db" / "triggers" / "trg_order_lines_audit.trg").read_text()
    out = run(text, "db/triggers/trg_order_lines_audit.trg")
    q = next(n for n in walk(units(out)["trg_order_lines_audit"]["body"]) if n["op"] == "query")
    assert [p["name"] for p in q["params"]] == ["nw.order_id", "nw.item_id"]
    assert [p["value"]["scope"] for p in q["params"]] == ["new", "new"]
    assert entities(out)[prog("trg_order_lines_audit")]["on"]["events"] == ["insert", "delete"]


def test_compound_trigger_sections():
    text = """CREATE OR REPLACE TRIGGER trg_lines_compound
  FOR INSERT OR UPDATE ON order_lines
  COMPOUND TRIGGER
  g_count PLS_INTEGER := 0;
  BEFORE STATEMENT IS
  BEGIN
    g_count := 0;
  END BEFORE STATEMENT;
  AFTER EACH ROW IS
  BEGIN
    g_count := g_count + 1;
  END AFTER EACH ROW;
  AFTER STATEMENT IS
  BEGIN
    UPDATE line_stats SET touched = touched + g_count;
  END AFTER STATEMENT;
END trg_lines_compound;
/
"""
    out = run(text, "db/trg_lines_compound.trg")
    attrs = entities(out)[prog("trg_lines_compound")]
    assert attrs["on"]["timing"] == "compound"
    assert attrs["sections"] == ["before statement", "after each row", "after statement"]
    unit = units(out)["trg_lines_compound"]
    assert validate_unit(unit) == []
    assert [n["detail"]["timing"] for n in unit["body"] if n["op"] == "platform"] == attrs["sections"]
    assert (prog("trg_lines_compound"), "WRITES_TO", table("line_stats")) in relations(out)


# ---------------------------------------------------------------------- install scripts


def test_grants_make_roles_grant_permissions():
    out = fixture("db/install/grants_and_jobs.sql")
    rels = relations(out)
    for name in ("EXECUTE on order_api", "SELECT on orders", "INSERT on orders", "UPDATE on orders"):
        assert (role("order_desk_role"), "GRANTS", perm(name)) in rels
    assert (role("auditor_role"), "GRANTS", perm("SELECT on order_events")) in rels
    assert (role("desk_app"), "GRANTS", role("order_desk_role")) in rels
    assert (role("desk_app"), "GRANTS", perm("CREATE SESSION")) in rels
    assert entities(out)[perm("EXECUTE on order_api")]["privilege"] == "EXECUTE"


def test_column_grants_and_grant_option():
    grant = "GRANT SELECT (id, total) ON orders TO reporting_role WITH GRANT OPTION;\n"
    # GRANT alone is standard SQL: a .sql file of grants only is not taken as Oracle
    assert run(grant, "db/g.sql").parsed is False
    out = run("CREATE SYNONYM order_rows FOR orders;\n" + grant, "db/g.sql")
    rows = [r for r in relation_rows(out) if r["predicate"] == "GRANTS"]
    assert {r["object"]["natural_key"]["name"] for r in rows} == {"SELECT on orders.id", "SELECT on orders.total"}
    assert all(r["qualifiers"] == {"grant_option": True} for r in rows)


def test_database_link_and_synonym_are_aliases_and_config_names():
    out = fixture("db/install/grants_and_jobs.sql")
    found = entities(out)
    link = key("ConfigKey", name="dblink:crm")
    assert found[link]["authentication"] == "current_user"
    rows = [r for r in relation_rows(out) if r["predicate"] == "ALIAS_OF"]
    assert [(ref(r["subject"]), ref(r["object"])) for r in rows] == [(table("crm_customers"), table("customers", "crm"))]
    assert rows[0]["qualifiers"] == {"via": "synonym"}
    assert rows[0]["subject"]["declare"] is False and rows[0]["object"]["declare"] is False


def test_a_synonym_for_a_local_object_also_aliases_the_program():
    out = run("CREATE PUBLIC SYNONYM order_api_pub FOR order_api;\n", "db/s.sql")
    pairs = {(ref(r["subject"]), ref(r["object"])) for r in relation_rows(out) if r["predicate"] == "ALIAS_OF"}
    assert pairs == {(table("order_api_pub"), table("order_api")), (prog("order_api_pub"), prog("order_api"))}


def test_code_across_a_database_link_reads_the_remote_table_and_names_the_link():
    out = fixture("db/packages/order_api.pkb")
    lrc = sym("order_api", "load_remote_customers")
    assert (lrc, "READS_FROM", table("customers", "crm")) in relations(out)
    assert (lrc, "REFERENCES_CONFIG", key("ConfigKey", name="dblink:crm")) in relations(out)
    assert not any("@" in k[1] for k in entities(out))


def test_scheduler_and_dbms_job_jobs_run_what_they_name():
    out = fixture("db/install/grants_and_jobs.sql")
    found, rels = entities(out), relations(out)
    nightly = key("Job", framework="dbms_scheduler", name="nightly_customer_sync")
    weekly = key("Job", framework="dbms_scheduler", name="weekly_cart_purge")
    legacy = key("Job", framework="dbms_job", name="order_api.archive_orders")
    assert {k: found[nightly][k] for k in ("job_type", "schedule", "enabled")} == {"job_type": "stored_procedure", "schedule": "FREQ=DAILY;BYHOUR=2", "enabled": True}
    assert found[weekly]["job_type"] == "plsql_block"
    assert found[legacy]["schedule"] == "TRUNC(SYSDATE) + 1"
    assert (nightly, "RUNS", sym("order_api", "load_remote_customers")) in rels
    assert (weekly, "RUNS", prog("purge_stale_carts")) in rels
    assert (legacy, "RUNS", sym("order_api", "archive_orders")) in rels
    # what a job runs is named, not declared: it resolves only to a unit some file declares
    assert all(r["object"]["declare"] is False for r in relation_rows(out) if r["predicate"] == "RUNS")


def test_sqlplus_commands_are_seen_not_modelled():
    out = fixture("db/install/grants_and_jobs.sql")
    assert constructs(out)[("ora:sqlplus", False)] == 4


def test_names_never_values():
    text = """CREATE USER batch_user IDENTIFIED BY "Sup3rSecret-fixture";
CREATE DATABASE LINK hq CONNECT TO hq_reader IDENTIFIED BY hq_pass_fixture_99 USING '(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=db-hq.internal.example)(PORT=1521))(CONNECT_DATA=(SERVICE_NAME=HQSVC)))';
ALTER USER batch_user IDENTIFIED BY other-fixture-pw;
CREATE OR REPLACE PROCEDURE copy_to_hq AS
BEGIN
  INSERT INTO stage.orders@hq (id) SELECT id FROM orders;
  -- mail ops@example.com when this fails
END;
/
"""
    out = run(text, "db/hq.sql")
    blob = (json.dumps([o.payload for o in out.observations]) + json.dumps(messages(out))).lower()
    for probe in ("sup3rsecret", "hq_pass_fixture", "other-fixture-pw", "db-hq.internal", "hqsvc", "1521", "ops@example.com"):
        assert probe not in blob, probe
    found = entities(out)
    assert found[key("ConfigKey", name="dblink:hq")]["authentication"] == "fixed_user"
    assert (prog("copy_to_hq"), "WRITES_TO", table("stage.orders", "hq")) in relations(out)


# ---------------------------------------------------------------------- what is not read


def test_wrapped_units_are_declared_and_not_read():
    text = "CREATE OR REPLACE PACKAGE BODY secret_api wrapped\na000000\n1\nabcd\nXk0Pq3sZ1bH9aGv2\n/\n"
    out = run(text, "db/secret_api.plb")
    assert entities(out)[prog("secret_api")]["wrapped"] is True
    assert constructs(out)[("ora:wrapped", False)] == 1
    assert not units(out)
    assert any("wrapped" in m for m in messages(out))


def test_a_unit_whose_structure_is_not_read_falls_back_to_lineage_and_shapes():
    text = """CREATE OR REPLACE PACKAGE BODY broken_api AS
  PROCEDURE p1 IS
  BEGIN
    UPDATE accounts SET balance = 0 WHERE id = 1;
    LOOP
      ))) not plsql (((
  END p1;
END broken_api;
/
"""
    out = run(text, "db/broken_api.pkb")
    assert (prog("broken_api"), "WRITES_TO", table("accounts")) in relations(out)
    assert not units(out)
    shapes = [o.payload for o in out.observations if o.kind == "transform"]
    assert len(shapes) == 1 and shapes[0]["language"] == "plsql"
    assert any("structure not read" in m for m in messages(out))
    assert constructs(out)[("ora:unrecognised_statement", False)] >= 1


def test_units_with_behaviour_leave_shapes_to_the_compiler():
    """Shapes come from the stored behaviour's query nodes (transforms.from_behavior),
    so a unit read into behaviour emits no transform observation of its own."""
    out = fixture("db/packages/order_api.pkb")
    assert not [o for o in out.observations if o.kind == "transform"]


def test_inventory_language_is_plsql():
    from repo_intel.extractors import detect_language

    for path in ("a.pks", "a.pkb", "a.fnc", "a.trg"):
        assert detect_language(path) == "plsql", path
