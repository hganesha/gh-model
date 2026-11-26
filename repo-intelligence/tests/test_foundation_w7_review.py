"""Foundation changes asked for by the W7 packs (docs/solution-ir-contract.md, section 10.3):
SQL lineage, transform shapes from behaviour, relation schema, construct kinds, rule
confirmations and secret detectors."""

from __future__ import annotations

import pytest

from repo_intel import platforms
from repo_intel.compile import transforms as tx
from repo_intel.compile.ontology import EntityKind as K, Predicate, check_relation
from repo_intel.extractors import sql_lineage
from repo_intel.ingest import secrets
from repo_intel.platforms import Construct, PlatformPack, Term
from repo_intel.storage.hashing import sha256_hex

# ------------------------------------------------------------------ sql_lineage


def test_oracle_create_or_replace_procedure_is_not_teradata():
    oracle = "CREATE OR REPLACE PROCEDURE load_orders AS\nBEGIN\n  INSERT INTO hist SELECT * FROM orders;\nEND;\n"
    assert sql_lineage.sql_platform("db/load.sql", oracle) == "sql"
    assert sql_lineage.sql_platform("db/load.sql", "  create or replace procedure p is begin null; end;") == "sql"
    teradata = "REPLACE PROCEDURE etl.load_orders ()\nBEGIN\n  INSERT INTO hist SELECT * FROM orders;\nEND;\n"
    assert sql_lineage.sql_platform("db/load.sql", teradata) == "teradata"
    assert sql_lineage.sql_platform("db/load.sql", "-- setup\n   replace procedure x () begin end;") == "teradata"


def test_tsql_update_through_a_from_alias_writes_the_bound_table():
    sql = "UPDATE o SET o.status = 'shipped' FROM sales.orders AS o JOIN sales.shipments AS s ON s.order_id = o.id"
    found = sql_lineage.lineage(sql, "tsql")
    assert [n for n, _, _ in found.writes] == ["sales.orders"]
    assert [n for n, _, _ in found.reads] == ["sales.shipments"], "the alias is not a table read"
    a, b = found.writes[0][1:]
    assert sql[a:b].endswith("orders")
    # the alias bound by a joined table
    found = sql_lineage.lineage("UPDATE o SET status = 1 FROM sales.shipments s JOIN sales.orders o ON s.order_id = o.id", "tsql")
    assert [n for n, _, _ in found.writes] == ["sales.orders"] and [n for n, _, _ in found.reads] == ["sales.shipments"]


def test_tsql_update_targets_that_are_not_aliases_stay_as_written():
    # a table updated by name, with an unrelated alias in FROM
    found = sql_lineage.lineage("UPDATE orders SET status = 1 FROM orders o2 WHERE o2.id = 1", "tsql")
    assert [n for n, _, _ in found.writes] == ["orders"]
    # a qualified target is a table, not an alias
    found = sql_lineage.lineage("UPDATE dbo.o SET status = 1 FROM sales.orders AS o", "tsql")
    assert [n for n, _, _ in found.writes] == ["dbo.o"]
    # an alias bound twice is ambiguous: kept as written, never guessed
    found = sql_lineage.lineage("UPDATE o SET status = 1 FROM a.orders o JOIN (SELECT id FROM b.orders o) x ON x.id = o.id", "tsql")
    assert [n for n, _, _ in found.writes] == ["a.orders"], "a table inside a subquery does not bind the outer alias"


def test_sql_scripts_extractor_writes_the_aliased_table():
    data = b"CREATE PROCEDURE dbo.ship_orders AS\nBEGIN\n  SET NOCOUNT ON;\n  UPDATE o SET o.status = 'shipped' FROM sales.orders AS o JOIN sales.shipments AS s ON s.order_id = o.id;\nEND\nGO\n"
    out = sql_lineage.SqlScripts().extract("db/ship.sql", data, sha256_hex(data))
    writes = {o.payload["object"]["natural_key"]["name"] for o in out.observations if o.kind == "relation" and o.payload["predicate"] == "WRITES_TO"}
    assert writes == {"sales.orders"}
    assert sql_lineage.SqlScripts.version == "1.2.0"


# ------------------------------------------------------------------ transforms.from_behavior


def _unit(language: str, sql: str) -> dict:
    return {"language": language, "kind": "function", "body": [{"op": "query", "at": [0, len(sql)], "sql": sql, "params": []}]}


def test_a_plsql_query_over_a_database_link_is_read_in_the_oracle_dialect():
    sql = "INSERT INTO local_orders (id, amount) SELECT o.id, o.amount FROM orders@sales_link o WHERE o.amount > 0"
    language, shapes = tx.from_behavior(_unit("plsql", sql))
    assert language == "plsql"
    assert not any(str(t.get("construct", "")).startswith("sql:unparsed") for t in shapes), shapes
    assert [t["op"] for t in shapes] == ["filter", "project"] and shapes[0]["inputs"] == ["orders@sales_link"]
    # read as generic SQL it does not parse
    language, shapes = tx.from_behavior(_unit("sql", sql))
    assert language == "sql" and shapes[0]["construct"] == "sql:unparsed_insert"


def test_shapes_from_behaviour_are_labelled_with_a_pack_language_else_sql():
    sql = "INSERT INTO hist SELECT * FROM orders"
    assert tx.from_behavior(_unit("plsql", sql))[0] == "plsql"
    assert tx.from_behavior(_unit("sql", sql))[0] == "sql"
    assert tx.from_behavior(_unit("not-a-pack", sql))[0] == "sql"


# ------------------------------------------------------------------ ontology


@pytest.mark.parametrize("triple, ok", [
    ((Predicate.CONSUMES, K.JOB, K.ENDPOINT), True),
    ((Predicate.CONSUMES, K.TASK, K.ENDPOINT), True),
    ((Predicate.CONSUMES, K.PROGRAM, K.ENDPOINT), True),
    ((Predicate.CONSUMES, K.AI_COMPONENT, K.MODEL_ENDPOINT), True),
    ((Predicate.CONSUMES, K.TABLE, K.ENDPOINT), False),
    ((Predicate.EXPOSES, K.JOB, K.ENDPOINT), True),
    ((Predicate.EXPOSES, K.PROGRAM, K.ENDPOINT), True),
    ((Predicate.EXPOSES, K.TASK, K.ENDPOINT), False),
])
def test_scheduler_jobs_consume_and_expose_transactions(triple, ok):
    assert (check_relation(*triple) is None) is ok


# ------------------------------------------------------------------ construct kinds


class _Extractor:
    id = "demo7.source"
    version = "1.0.0"

    def matches(self, path):
        return False

    def extract(self, path, data, blob):
        raise AssertionError


def _pack(*constructs: Construct) -> PlatformPack:
    return PlatformPack(id="demo7", name="demo", prefix="d7", version="1.0.0", terms=(Term("d7:Job", K.JOB, "lossy", ""),), constructs=constructs, extractors=(_Extractor(),))


def test_a_construct_may_be_modelled_as_a_transform_op():
    assert platforms.validate_pack(_pack(Construct("d7:lookup", "a lookup stage", "transform:join"), Construct("d7:sort", "", "transform:sort"))) == []
    problems = " | ".join(platforms.validate_pack(_pack(Construct("d7:x", "", "transform:shuffle"), Construct("d7:y", "", "guess:join"))))
    assert "unknown transform op 'shuffle'" in problems
    assert "modelled_as 'guess:join'" in problems


# ------------------------------------------------------------------ decisions


def test_document_rules_take_confirmations():
    from repo_intel.decisions.overrides import CONFIRMATION_SPECS, DecisionOverride

    assert "dm.document-rules" in CONFIRMATION_SPECS
    scope = "ent_" + "a" * 26
    with pytest.raises(ValueError):
        DecisionOverride(spec="dm.document-rules", scope_entity=scope, label="maybe")
    assert DecisionOverride(spec="dm.document-rules", scope_entity=scope, label="confirmed").label == "confirmed"


def test_answered_rules_is_public_and_the_old_name_still_works():
    from repo_intel.decisions import rules as dm_rules

    assert dm_rules._answered_rules is dm_rules.answered_rules
    assert dm_rules.answered_rules({"rules": "none"}, {}) == []


# ------------------------------------------------------------------ secret detectors


def _flagged(data: bytes) -> list[tuple[str, bytes]]:
    return [(f["detector"], data[f["start_byte"]:f["end_byte"]]) for f in secrets.scan(data)]


def test_oracle_identified_by_passwords_are_found():
    script = b"""CREATE USER app_owner IDENTIFIED BY fixture-not-a-real-password-0028;
CREATE DATABASE LINK sales_link CONNECT TO reader IDENTIFIED BY "Xk9#pQ2vLm" USING 'SALES';
ALTER USER reporter identified   by S3cure_pass_2024
  DEFAULT TABLESPACE users;
"""
    assert _flagged(script) == [
        ("oracle_identified_by", b"fixture-not-a-real-password-0028"),
        ("oracle_identified_by", b"Xk9#pQ2vLm"),
        ("oracle_identified_by", b"S3cure_pass_2024"),
    ]
    # a password hash, substitution and bind variables, a short word and dynamic SQL are not flagged
    for text in (b"ALTER USER a IDENTIFIED BY VALUES 'S:0123456789ABCDEF0123456789ABCDEF';", b"CREATE USER a IDENTIFIED BY &app_password;",
                 b"ALTER USER a IDENTIFIED BY :new_password", b"CREATE USER scott IDENTIFIED BY tiger;",
                 b"EXECUTE IMMEDIATE 'ALTER USER ' || p_user || ' IDENTIFIED BY ' || p_password;", b"CREATE USER ops IDENTIFIED EXTERNALLY;"):
        assert secrets.scan(text) == [], text


def test_parameter_file_credentials_are_found():
    par = b"""[Global]
$$LOAD_DATE=2024-01-31
$$DB_PASSWORD=fixture-not-a-real-password-0031
[SALES.WF:wf_daily_sales.ST:s_m_load_sales]
$DBConnection_DW=ORA_DW
  $Param_SftpPwd = Tr0ub4dor&3x
$$SRC_PASSWORD=$PMSourcePassword
$$API_SECRET=short
"""
    assert _flagged(par) == [("parameter_file_credential", b"fixture-not-a-real-password-0031"), ("parameter_file_credential", b"Tr0ub4dor&3x")]
    # not parameter lines: a query string, an expression in code
    for text in (b"https://x/login?user=a&password=s3cret-value-123", b"password = make_password(user)\n"):
        assert secrets.scan(text) == [], text


def test_masked_parameter_file_keeps_names_and_offsets():
    par = b"$$DB_USER=etl\n$$DB_PASSWORD=fixture-not-a-real-password-0031\n"
    clean = secrets.mask_for_extraction(par, secrets.scan(par))
    assert len(clean) == len(par) and b"fixture-not-a-real-password" not in clean and b"$$DB_PASSWORD=" in clean and b"$$DB_USER=etl" in clean
    assert secrets.SCAN_VERSION == "4"
