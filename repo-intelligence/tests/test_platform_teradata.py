"""The teradata pack (repo_intel/platforms/teradata/): FastLoad, MultiLoad, FastExport and
TPT job scripts, each read from one file's bytes. BTEQ stays with `data.sql_lineage`."""

import hashlib
import json
from pathlib import Path

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import validate_unit
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.compile.transforms import validate_shape
from repo_intel.platforms import CONSTRUCTS, packs, validate_pack
from repo_intel.platforms.teradata import PACK
from repo_intel.platforms.teradata.utilities import Utilities, utility

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "F29-warehouse-shell"


def run(path: str, data: bytes | str):
    raw = data.encode() if isinstance(data, str) else data
    return Utilities().extract(path, raw, hashlib.sha256(raw).hexdigest())


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


def relations(out) -> set[tuple]:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])) for o in out.observations if o.kind == "relation"}


def constructs(out) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = {}
    for o in out.observations:
        if o.kind == "construct":
            k = (o.payload["construct"], o.payload["modelled"])
            counts[k] = counts.get(k, 0) + 1
    return counts


def text_of(out) -> str:
    return json.dumps([o.payload for o in out.observations], sort_keys=True) + json.dumps([d.message for d in out.diagnostics])


def check_all(out) -> None:
    for o in out.observations:
        if o.kind == "behavior":
            assert validate_unit(o.payload) == []
        if o.kind == "transform":
            assert validate_shape(o.payload) == [], validate_shape(o.payload)
        if o.kind == "relation":
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p
        if o.kind == "construct":
            assert o.payload["construct"] in CONSTRUCTS
    text = text_of(out)
    for leak in ("tdprod", "etl_user", "TD_LOGON_SECRET", "s3cr3t-logon", "UserPassword"):
        assert leak not in text, leak


FASTLOAD = """/* stage the orders file */
SESSIONS 4;
ERRLIMIT 25;
LOGON tdprod/etl_user,s3cr3t-logon;
DATABASE stg;
DROP TABLE orders_e1;
SET RECORD VARTEXT ",";
DEFINE order_id (VARCHAR(18)),
       amount (VARCHAR(20))
FILE = /data/in/orders.csv;
BEGIN LOADING orders ERRORFILES orders_e1, orders_e2 CHECKPOINT 100000;
INSERT INTO orders (order_id, amount) VALUES (:order_id, :amount);
END LOADING;
LOGOFF;
"""

MULTILOAD = """.LOGTABLE stg.customers_mlog;
.LOGON tdprod/etl_user,s3cr3t-logon;
.SET region TO 'emea';
.ACCEPT run_tag FROM ENV RUN_TAG;
.BEGIN IMPORT MLOAD TABLES stg.customers WORKTABLES stg.wt_customers ERRORTABLES stg.et_customers stg.uv_customers;
.LAYOUT cust_layout;
.FIELD customer_id * VARCHAR(18);
.FIELD name * VARCHAR(60);
.FILLER junk * VARCHAR(2);
.DML LABEL upsert_cust DO INSERT FOR MISSING UPDATE ROWS;
UPDATE stg.customers SET name = :name WHERE customer_id = :customer_id;
INSERT INTO stg.customers (customer_id, name) VALUES (:customer_id, :name);
.DML LABEL tag_rows;
INSERT INTO stg.cust_&run_tag (customer_id) VALUES (:customer_id);
.IMPORT INFILE /data/in/customers_&region..txt FORMAT VARTEXT '|' LAYOUT cust_layout APPLY upsert_cust APPLY tag_rows;
.END MLOAD;
.LOGOFF;
"""

FASTEXPORT = """.LOGTABLE stg.exp_log;
.LOGON tdprod/etl_user,s3cr3t-logon;
.BEGIN EXPORT SESSIONS 4;
.EXPORT OUTFILE /data/out/active.dat MODE RECORD FORMAT TEXT;
SELECT c.customer_id, SUM(o.amount) FROM stg.customers c JOIN stg.orders o ON o.customer_id = c.customer_id GROUP BY c.customer_id;
.END EXPORT;
.LOGOFF;
"""

TPT = """DEFINE JOB load_orders
(
  DEFINE SCHEMA orders_schema (order_id VARCHAR(18), amount VARCHAR(20));
  DEFINE OPERATOR file_reader
  TYPE DATACONNECTOR PRODUCER
  SCHEMA orders_schema
  ATTRIBUTES (VARCHAR DirectoryPath = '/data/in/', VARCHAR FileName = 'orders.csv', VARCHAR Format = 'Delimited');
  DEFINE OPERATOR load_op
  TYPE LOAD
  SCHEMA *
  ATTRIBUTES (VARCHAR TdpId = @TdpId, VARCHAR UserName = @UserName, VARCHAR UserPassword = 's3cr3t-logon',
              VARCHAR TargetTable = 'stg.orders', VARCHAR LogTable = 'stg.orders_log');
  DEFINE OPERATOR export_op
  TYPE EXPORT
  SCHEMA *
  ATTRIBUTES (VARCHAR TdpId = @TdpId, VARCHAR SelectStmt = @ExportQuery);
  APPLY ('INSERT INTO stg.orders (order_id, amount) VALUES (:order_id, :amount);')
  TO OPERATOR (load_op)
  SELECT * FROM OPERATOR (file_reader);
);
"""


def test_pack_is_registered_and_valid():
    assert validate_pack(PACK) == []
    assert packs()["teradata"] is PACK and PACK.prefix == "td"
    assert [e.id for e in PACK.extractors] == ["teradata.utilities"]
    assert Utilities.supersedes_when_parsed == ("data.sql_lineage",)
    # BTEQ is not this pack's: sql_lineage reads it
    assert not Utilities().matches("bteq/load.bteq") and Utilities().matches("td/load.tpt") and Utilities().matches("td/x.fexp")


def test_utility_is_recognised_by_content():
    assert utility(FASTLOAD) == "fastload" and utility(MULTILOAD) == "multiload" and utility(FASTEXPORT) == "fastexport" and utility(TPT) == "tpt"
    assert run("td/notes.mload", "-- nothing to load here\nSELECT 1;\n").parsed is False


def test_fastload():
    path = "teradata/orders.fastload"
    out = run(path, FASTLOAD)
    check_all(out)
    ents, rels = entities(out), relations(out)
    me = key("Program", platform="teradata", name=path)
    assert ents[me]["platform_kind"] == "td:FastLoadScript" and ents[me]["utility"] == "fastload"
    assert ents[me]["default_database"] == "stg" and ents[me]["format"] == "vartext"
    assert ents[me]["error_tables"] == ["stg.orders_e1", "stg.orders_e2"]
    assert (me, "WRITES_TO", key("Table", datastore="default", name="stg.orders")) in rels
    assert (me, "READS_FROM", key("Dataset", name="/data/in/orders.csv")) in rels
    layout = key("DataSchema", kind="record_layout", name=f"teradata:{path}")
    assert ents[layout]["platform_kind"] == "td:RecordLayout" and (me, "DEPENDS_ON", layout) in rels
    assert ents[key("Field", table=f"teradata:{path}", name="amount")]["type"] == "VARCHAR(20)"
    assert not any(k[0] == "Table" and "orders_e" in k[1] for k in ents)  # error tables are attributes
    c = constructs(out)
    assert c[("td:begin-loading", True)] == 1 and c[("td:field", True)] == 2 and c[("td:import", True)] == 1
    assert c[("td:logon", False)] == 1 and c[("td:sessions", False)] == 2
    assert [o.payload for o in out.observations if o.kind == "transform"]


def test_multiload_upsert_and_support_variables():
    path = "teradata/customers.mload"
    out = run(path, MULTILOAD)
    check_all(out)
    ents, rels = entities(out), relations(out)
    me = key("Program", platform="teradata", name=path)
    assert ents[me]["platform_kind"] == "td:MultiLoadScript" and ents[me]["log_table"] == "stg.customers_mlog"
    assert ents[me]["work_tables"] == ["stg.wt_customers"] and ents[me]["error_tables"] == ["stg.et_customers", "stg.uv_customers"]
    assert (me, "WRITES_TO", key("Table", datastore="default", name="stg.customers")) in rels
    # .SET region TO 'emea' is certain; run_tag is ACCEPTed, so its table stays dynamic
    assert (me, "READS_FROM", key("Dataset", name="/data/in/customers_emea.txt")) in rels
    assert not any(k[0] == "Table" and "cust_" in k[1] for k in ents)
    assert any("variable whose value is not in the file" in d.message for d in out.diagnostics)
    assert ents[key("Field", table="teradata:cust_layout", name="junk")]["filler"] is True
    c = constructs(out)
    assert c[("td:upsert", True)] == 1 and c[("td:dml-label", True)] == 2 and c[("td:apply", True)] == 2
    assert c[("td:dynamic", False)] == 1 and c[("td:support-set", False)] == 2
    shapes = [o.payload for o in out.observations if o.kind == "transform"]
    assert shapes and any(t.get("primitive") == "merge" or "merge" in json.dumps(t) for s in shapes for t in s["transforms"])


def test_fastexport():
    path = "teradata/active.fexp"
    out = run(path, FASTEXPORT)
    check_all(out)
    ents, rels = entities(out), relations(out)
    me = key("Program", platform="teradata", name=path)
    assert ents[me]["platform_kind"] == "td:FastExportScript" and ents[me]["format"] == "text"
    assert (me, "WRITES_TO", key("Dataset", name="/data/out/active.dat")) in rels
    assert (me, "READS_FROM", key("Table", datastore="default", name="stg.customers")) in rels
    assert (me, "READS_FROM", key("Table", datastore="default", name="stg.orders")) in rels
    c = constructs(out)
    assert c[("td:export", True)] == 1 and c[("td:select", True)] == 1 and c[("td:logtable", True)] == 1


def test_tpt_job():
    path = "teradata/load_orders.tpt"
    out = run(path, TPT)
    check_all(out)
    ents, rels = entities(out), relations(out)
    me = key("Program", platform="teradata", name=path)
    assert ents[me]["platform_kind"] == "td:TptJob" and ents[me]["job"] == "load_orders" and ents[me]["log_tables"] == ["stg.orders_log"]
    assert (me, "READS_FROM", key("Dataset", name="/data/in/orders.csv")) in rels
    assert (me, "WRITES_TO", key("Table", datastore="default", name="stg.orders")) in rels
    assert (me, "DEPENDS_ON", key("DataSchema", kind="record_layout", name="teradata:orders_schema")) in rels
    c = constructs(out)
    assert c[("td:tpt.job", True)] == 1 and c[("td:tpt.schema", True)] == 1 and c[("td:tpt.apply", True)] == 1
    assert c[("td:tpt.operator.load", True)] == 1 and c[("td:tpt.operator.file", True)] == 1
    assert c[("td:tpt.operator.export", False)] == 1  # its SELECT is a job variable: dynamic
    assert c[("td:logon", False)] == 2 and c[("td:tpt.job-variable", False)] == 1
    assert any("job variable" in d.message for d in out.diagnostics)


def test_fixture_scripts():
    for path, term in (("teradata/orders.fastload", "td:FastLoadScript"), ("teradata/customers.mload", "td:MultiLoadScript"),
                       ("teradata/active_customers.fexp", "td:FastExportScript"), ("teradata/load_orders.tpt", "td:TptJob")):
        out = run(path, (FIXTURE / "base" / path).read_bytes())
        check_all(out)
        assert entities(out)[key("Program", platform="teradata", name=path)]["platform_kind"] == term
        assert not [o for o in out.observations if o.kind == "behavior"]  # load scripts are lineage and shapes, not behaviour
