"""The dbproc pack (repo_intel/platforms/dbproc/): DB2 SQL PL, Netezza NZPLSQL and Sybase ASE
procedures, functions and triggers, each read from one file's bytes, with one behaviour unit
per routine (language db2, netezza or sybase)."""

import hashlib
import json
from pathlib import Path

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import validate_unit, walk
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.platforms import CONSTRUCTS, packs, validate_pack
from repo_intel.platforms.dbproc import PACK
from repo_intel.platforms.dbproc.source import Routines, engine_of

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "F29-warehouse-shell"


def run(path: str, data: bytes | str):
    raw = data.encode() if isinstance(data, str) else data
    return Routines().extract(path, raw, hashlib.sha256(raw).hexdigest())


def ref(x) -> tuple:
    return x if isinstance(x, str) else (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def key(_kind: str, **nk) -> tuple:
    return (_kind, json.dumps(nk, sort_keys=True))


def entities(out) -> dict[tuple, dict]:
    found: dict[tuple, dict] = {}
    for o in out.observations:
        if o.kind == "entity":
            found.setdefault(ref(o.payload), {}).update(o.payload["attributes"])
    return found


def relations(out) -> dict[tuple, dict]:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])): o.payload["object"] for o in out.observations if o.kind == "relation"}


def constructs(out) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = {}
    for o in out.observations:
        if o.kind == "construct":
            k = (o.payload["construct"], o.payload["modelled"])
            counts[k] = counts.get(k, 0) + 1
    return counts


def units(out) -> dict[str, dict]:
    return {o.payload["owner"]["natural_key"]["name"]: o.payload for o in out.observations if o.kind == "behavior"}


def ops(unit: dict) -> list[str]:
    return [n.get("op") for n in walk(unit["body"]) if "op" in n]


def check_all(out) -> None:
    for o in out.observations:
        if o.kind == "behavior":
            assert validate_unit(o.payload) == [], validate_unit(o.payload)
            for n in walk(o.payload["body"]):
                if n.get("op") == "platform" or n.get("e") == "platform":
                    assert n["construct"] in CONSTRUCTS, n["construct"]
        if o.kind == "relation":
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p
        if o.kind == "construct":
            assert o.payload["construct"] in CONSTRUCTS


DB2 = """--#SET TERMINATOR @
CREATE OR REPLACE PROCEDURE FIN.REFRESH (IN p_day DATE, OUT p_rows INTEGER)
  SPECIFIC refresh_v1
  DYNAMIC RESULT SETS 1
  LANGUAGE SQL
P1: BEGIN
  DECLARE v_count INTEGER DEFAULT 0;
  DECLARE v_sql VARCHAR(200);
  DECLARE v_url VARCHAR(200);
  DECLARE c_out CURSOR WITH RETURN FOR SELECT account_id FROM fin.balances WHERE as_of = p_day;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
      ROLLBACK;
      RESIGNAL;
    END;
  SET v_url = 'https://etl:hunter2-not-real@api.example.com/notify?token=abc123';
  DELETE FROM fin.balances WHERE as_of = p_day;
  INSERT INTO fin.balances (account_id, as_of, balance)
    SELECT account_id, p_day, SUM(amount) FROM fin.transactions GROUP BY account_id;
  GET DIAGNOSTICS v_count = ROW_COUNT;
  SET p_rows = v_count;
  IF v_count = 0 THEN
    CALL fin.alert('empty');
  END IF;
  FOR r AS c1 CURSOR FOR SELECT account_id FROM fin.accounts WHERE closed = 'Y' DO
    CALL fin.archive_account(r.account_id);
  END FOR;
  REPEAT
    SET v_count = v_count - 1;
  UNTIL v_count <= 0 END REPEAT;
  SET v_sql = 'DELETE FROM fin.log_' || CHAR(p_day);
  EXECUTE IMMEDIATE v_sql;
  COMMIT;
  OPEN c_out;
END P1
@

CREATE TRIGGER fin.trg_audit
  AFTER UPDATE OF balance ON fin.balances
  REFERENCING OLD AS o NEW AS n
  FOR EACH ROW MODE DB2SQL
  WHEN (n.balance < 0)
BEGIN ATOMIC
  INSERT INTO fin.balance_audit (account_id, old_balance) VALUES (n.account_id, o.balance);
END
@
"""

NETEZZA = """CREATE OR REPLACE PROCEDURE mart.load_day(DATE, VARCHAR(10))
RETURNS INTEGER
LANGUAGE NZPLSQL AS
BEGIN_PROC
DECLARE
  p_day ALIAS FOR $1;
  p_region ALIAS FOR $2;
  v_rows INTEGER := 0;
BEGIN
  INSERT INTO mart.daily (day, region, total)
    SELECT day, region, SUM(amount) FROM stage.sales WHERE day = $1 AND region = $2 GROUP BY day, region;
  v_rows := ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE NOTICE 'no rows for %', p_day;
  END IF;
  WHILE v_rows > 0 LOOP
    v_rows := v_rows - 1;
  END LOOP;
  EXECUTE IMMEDIATE 'GROOM TABLE mart.daily_' || p_region;
  CALL mart.refresh_summary(p_day);
  RETURN v_rows;
EXCEPTION
  WHEN OTHERS THEN
    RAISE EXCEPTION 'load failed: %', SQLERRM;
END;
END_PROC;
"""

SYBASE = """create procedure dbo.post_day
    @day datetime,
    @desk varchar(10) = 'ALL',
    @posted int output
as
begin
    declare @count int
    set nocount on
    select @count = count(*) from trades holdlock where trade_date = @day
    if @count = 0
    begin
        print "nothing for %1!", @day
        return 1
    end
    begin tran
    insert into positions (desk, qty) select desk, sum(qty) from trades where trade_date = @day group by desk
    if @@error != 0
    begin
        rollback tran
        raiserror 20001 "posting failed"
        return 2
    end
    exec @posted = dbo.count_posted @day
    commit tran
    select desk, qty from positions where desk = @desk
end
go
create trigger dbo.trg_trades on dbo.trades for insert, update as
    insert into trade_audit (trade_id) select trade_id from inserted
go
"""


def test_pack_is_registered_and_valid():
    assert validate_pack(PACK) == []
    assert packs()["dbproc"] is PACK and PACK.prefix == "dbp"
    assert [e.id for e in PACK.extractors] == ["dbproc.routines"]
    # per-file supersession: a DB2 .sql procedure is not also Program {sql}
    assert {"data.sql_lineage", "data.sql_ddl"} <= set(Routines.supersedes_when_parsed)
    ids = {e.id for e in extractors.EXTRACTORS}
    assert set(Routines.supersedes_when_parsed) <= ids


def test_engine_is_recognised_by_content():
    assert engine_of("db/refresh.sql", DB2) == "db2"
    assert engine_of("nz/load.nzpl", NETEZZA) == "netezza"
    assert engine_of("db/post.sql", SYBASE) == "sybase"
    oracle = "CREATE OR REPLACE PROCEDURE p (x IN VARCHAR2) AS BEGIN NULL; END;\n/\n"
    sqlserver = "CREATE PROCEDURE dbo.p AS BEGIN TRY SELECT 1 END TRY BEGIN CATCH END CATCH\nGO\n"
    assert engine_of("db/p.sql", oracle) is None and engine_of("db/p.sql", sqlserver) is None
    assert run("db/p.sql", oracle).parsed is False
    assert engine_of("db/tables.sql", "CREATE TABLE t (a INT);") is None


def test_db2_procedure_and_trigger():
    path = "db2/refresh.sql"
    out = run(path, DB2)
    check_all(out)
    ents, rels = entities(out), relations(out)
    me = key("Program", platform="db2", name="fin.refresh")
    assert ents[me]["platform_kind"] == "dbp:Procedure" and ents[me]["platform"] == "dbproc"
    assert ents[me]["language"] == "sqlpl" and ents[me]["specific"] == "refresh_v1" and ents[me]["result_sets"] == 1
    assert ents[me]["params"] == ["p_day", "p_rows"]
    for name in ("fin.balances",):
        assert (me, "WRITES_TO", key("Table", datastore="default", name=name)) in rels
    for name in ("fin.transactions", "fin.accounts", "fin.balances"):
        assert (me, "READS_FROM", key("Table", datastore="default", name=name)) in rels
    for callee in ("fin.alert", "fin.archive_account"):
        assert rels[(me, "CALLS", key("Program", platform="db2", name=callee))]["declare"] is False
    assert not any(k[0] == "Table" and "fin.log_" in k[1] for k in ents)  # dynamic SQL is not read for tables
    assert any("built at run time" in d.message for d in out.diagnostics)
    trg = key("Program", platform="db2", name="fin.trg_audit")
    assert ents[trg]["platform_kind"] == "dbp:Trigger" and ents[trg]["on"] == {"table": "fin.balances", "events": ["update"], "timing": "after"}
    assert (trg, "WRITES_TO", key("Table", datastore="default", name="fin.balance_audit")) in rels
    u = units(out)
    assert u["fin.refresh"]["language"] == "db2" and u["fin.refresh"]["kind"] == "function"
    assert [p["name"] for p in u["fin.refresh"]["params"]] == ["p_day", "p_rows"] and u["fin.refresh"]["params"][1]["direction"] == "out"
    assert {"try", "query", "if", "loop", "call", "transaction", "throw", "assign", "declare"} <= set(ops(u["fin.refresh"]))
    assert u["fin.trg_audit"]["kind"] == "trigger" and u["fin.trg_audit"]["body"][0]["op"] == "if"
    c = constructs(out)
    assert c[("dbp:procedure", True)] == 1 and c[("dbp:trigger", True)] == 1 and c[("dbp:parameter", True)] == 2
    assert c[("dbp:handler", True)] == 1 and c[("dbp:declare-cursor", True)] == 1 and c[("dbp:for", True)] == 1 and c[("dbp:repeat", True)] == 1
    assert c[("dbp:call", True)] == 2 and c[("dbp:dynamic-sql", False)] == 1 and c[("dbp:get-diagnostics", False)] == 1
    # URLs keep scheme, host, path and parameter names only (contract 2.7)
    text = json.dumps([o.payload for o in out.observations])
    assert "hunter2-not-real" not in text and "abc123" not in text


def test_netezza_procedure():
    path = "netezza/load_day.nzpl"
    out = run(path, NETEZZA)
    check_all(out)
    ents, rels = entities(out), relations(out)
    me = key("Program", platform="netezza", name="mart.load_day")
    assert ents[me]["platform_kind"] == "dbp:Procedure" and ents[me]["language"] == "nzplsql" and ents[me]["returns"] == "integer"
    assert ents[me]["params"] == ["p_day", "p_region"]  # named by ALIAS FOR $n
    assert (me, "WRITES_TO", key("Table", datastore="default", name="mart.daily")) in rels
    assert (me, "READS_FROM", key("Table", datastore="default", name="stage.sales")) in rels
    assert rels[(me, "CALLS", key("Program", platform="netezza", name="mart.refresh_summary"))]["declare"] is False
    u = units(out)["mart.load_day"]
    assert u["language"] == "netezza" and u["kind"] == "function"
    query = next(n for n in walk(u["body"]) if n.get("op") == "query")
    assert ":p_day" in query["sql"] and {p["name"] for p in query["params"]} == {"p_day", "p_region"}
    assert {"try", "output", "loop", "throw", "return", "call"} <= set(ops(u))
    c = constructs(out)
    assert c[("dbp:alias", True)] == 2 and c[("dbp:exception-block", True)] == 1 and c[("dbp:while", True)] == 1
    assert c[("dbp:print", True)] == 1 and c[("dbp:signal", True)] == 1 and c[("dbp:dynamic-sql", False)] == 1


def test_sybase_procedure_and_trigger():
    path = "sybase/post_day.sql"
    out = run(path, SYBASE)
    check_all(out)
    ents, rels = entities(out), relations(out)
    me = key("Program", platform="sybase", name="dbo.post_day")
    assert ents[me]["platform_kind"] == "dbp:Procedure" and ents[me]["language"] == "tsql" and ents[me]["params"] == ["day", "desk", "posted"]
    assert (me, "READS_FROM", key("Table", datastore="default", name="trades")) in rels
    assert (me, "WRITES_TO", key("Table", datastore="default", name="positions")) in rels
    assert rels[(me, "CALLS", key("Program", platform="sybase", name="dbo.count_posted"))]["declare"] is False
    # supersession keeps sql_lineage off the file, so there is no alias under the platform it guesses
    assert not any(r[1] == "ALIAS_OF" for r in rels)
    trg = key("Program", platform="sybase", name="dbo.trg_trades")
    assert ents[trg]["on"] == {"table": "dbo.trades", "events": ["insert", "update"], "timing": "after"}
    assert (trg, "WRITES_TO", key("Table", datastore="default", name="trade_audit")) in rels
    assert not any(k[0] == "Table" and "inserted" in k[1] for k in ents)  # a trigger's pseudo table is not a table
    u = units(out)
    params = {p["name"]: p for p in u["dbo.post_day"]["params"]}
    assert params["desk"]["required"] is False and params["posted"]["direction"] == "out"
    q = [n for n in walk(u["dbo.post_day"]["body"]) if n.get("op") == "query"]
    assert q[0]["target"] == {"e": "var", "scope": "local", "name": "count"} and ":day" in q[0]["sql"]
    assert u["dbo.trg_trades"]["kind"] == "trigger"
    c = constructs(out)
    assert c[("dbp:transaction", True)] == 3 and c[("dbp:session-option", False)] == 1 and c[("dbp:signal", True)] == 1
    assert c[("dbp:result-sets", True)] == 1


def test_fixture_routines():
    expect = {"db2/refresh_balances.sql": {("db2", "fin.refresh_balances"), ("db2", "fin.trg_balance_audit")},
              "netezza/load_daily_sales.nzpl": {("netezza", "mart.load_daily_sales")},
              "sybase/post_trades.sql": {("sybase", "dbo.post_trades"), ("sybase", "dbo.trg_trades_ins")}}
    for path, programs in expect.items():
        out = run(path, (FIXTURE / "base" / path).read_bytes())
        check_all(out)
        declared = {(json.loads(k[1])["platform"], json.loads(k[1])["name"]) for k, a in entities(out).items() if k[0] == "Program"}
        assert declared == programs
        assert {u["owner"]["natural_key"]["name"] for u in units(out).values()} == {n for _, n in programs}
