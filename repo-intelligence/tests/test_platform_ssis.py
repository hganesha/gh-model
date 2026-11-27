"""The SSIS pack (repo_intel.platforms.ssis): packages, project files and project deployment
archives read from their bytes into Jobs, Tasks, ConfigKeys, transform shapes and behaviour
units, with names and never values."""

import hashlib
import importlib.util
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the packs)
from repo_intel.compile.behavior import validate_unit
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.compile.transforms import validate_shape
from repo_intel.ingest import secrets
from repo_intel.platforms import packs, validate_pack
from repo_intel.platforms.ssis import PACK, xmlread
from repo_intel.platforms.ssis.source import SsisPackageSource, SsisProjectArchive, SsisProjectFiles

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "F30-ssis-package"
PKG = Path(__file__).resolve().parents[1] / "repo_intel" / "platforms" / "ssis"
LOAD = "etl/SalesLoad/LoadSales.dtsx"
PUBLISH = "etl/SalesLoad/Publish.dtsx"
ISPAC = "deploy/SalesLoad.ispac"
DATE = (2026, 1, 1, 0, 0, 0)
NEVER = ("dwsql01", "fileserver01", "etl_loader", "fixture-not-a-real-password", "SalesDW", "example.invalid", "orders.csv", "SQLNCLI", "mart.sales_current")


def admitted(data: bytes) -> bytes:
    return secrets.mask_for_extraction(data, secrets.scan(data))


def layer_bytes(path: str, layer: str = "base") -> bytes:
    return (FIXTURE / layer / path).read_bytes()


def run(extractor, path: str, data: bytes):
    data = admitted(data)
    return extractor.extract(path, data, hashlib.sha256(data).hexdigest())


def package(path: str = LOAD, layer: str = "base", data: bytes | None = None):
    return run(SsisPackageSource(), path, data if data is not None else layer_bytes(path, layer))


def archive():
    return run(SsisProjectArchive(), ISPAC, layer_bytes(ISPAC, "commits/02-deployment-archive"))


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


def relations(out) -> dict[tuple, dict]:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])): o.payload["qualifiers"] for o in out.observations if o.kind == "relation"}


def units(out, kind: str) -> list[dict]:
    return [o.payload for o in out.observations if o.kind == kind]


def unit_of(out, kind: str, name: str) -> dict:
    return next(u for u in units(out, kind) if u["owner"]["natural_key"]["name"] == name)


def constructs(out) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = {}
    for o in out.observations:
        if o.kind == "construct":
            k = (o.payload["construct"], o.payload["modelled"])
            counts[k] = counts.get(k, 0) + 1
    return counts


def messages(out) -> list[str]:
    return [d.message for d in out.diagnostics]


def job(name: str = LOAD) -> tuple:
    return key("Job", framework="ssis", name=name)


def task(name: str, job_name: str = LOAD) -> tuple:
    return key("Task", framework="ssis", job=job_name, name=name)


def prog(name: str) -> tuple:
    return key("Program", platform="ssis", name=name)


def table(name: str) -> tuple:
    return key("Table", datastore="default", name=name)


def cfg(name: str) -> tuple:
    return key("ConfigKey", name=name)


def blob(out) -> str:
    return json.dumps([o.payload for o in out.observations], default=str) + json.dumps(messages(out))


def check_contract(out, data: bytes) -> None:
    for u in units(out, "behavior"):
        assert validate_unit(u) == [], u
    for u in units(out, "transform"):
        assert validate_shape(u) == [], u
    for o in out.observations:
        s = o.evidence.span
        assert 0 <= s.start_byte <= s.end_byte <= len(data), o.payload
        if o.kind == "relation" and isinstance(o.payload["subject"], dict):
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p


def dtsx(executables: str, connections: str = "", constraints: str = "", head: str = "") -> bytes:
    return (
        '<?xml version="1.0"?>' + head
        + '<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:refId="Package" DTS:ExecutableType="Microsoft.Package" DTS:ObjectName="T">'
        + f"<DTS:ConnectionManagers>{connections}</DTS:ConnectionManagers><DTS:Executables>{executables}</DTS:Executables>"
        + f"<DTS:PrecedenceConstraints>{constraints}</DTS:PrecedenceConstraints></DTS:Executable>"
    ).encode()


def sql_task(name: str, sql: str, extra: str = "", source_type: str = "") -> str:
    st = f' SQLTask:SqlStmtSourceType="{source_type}"' if source_type else ""
    return (f'<DTS:Executable DTS:refId="Package\\{name}" DTS:ExecutableType="Microsoft.ExecuteSQLTask" DTS:ObjectName="{name}"><DTS:ObjectData>'
            f'<SQLTask:SqlTaskData xmlns:SQLTask="www.microsoft.com/sqlserver/dts/tasks/sqltask" SQLTask:Connection="DB"{st} SQLTask:SqlStatementSource="{sql}">{extra}</SQLTask:SqlTaskData>'
            "</DTS:ObjectData></DTS:Executable>")


OLEDB = ('<DTS:ConnectionManager DTS:refId="Package.ConnectionManagers[DB]" DTS:CreationName="OLEDB" DTS:DTSID="DB" DTS:ObjectName="DB">'
         '<DTS:ObjectData><DTS:ConnectionManager DTS:ConnectionString="Data Source=db01.example.invalid;Password=fixture-not-a-real-password-9;" /></DTS:ObjectData></DTS:ConnectionManager>')


# ---------------------------------------------------------------------- registration


def test_registered_and_valid():
    assert packs()["ssis"] is PACK
    assert validate_pack(PACK) == []
    assert {e.id for e in PACK.extractors} == {"ssis.package", "ssis.project", "ssis.ispac"}
    assert {t.name for t in PACK.terms} >= {"ssis:Package", "ssis:Task", "ssis:DataFlow", "ssis:SqlTask", "ssis:ConnectionManager", "ssis:Parameter", "ssis:FlatFileLayout", "ssis:FlatFileColumn"}
    assert SsisPackageSource().matches("a/B.DTSX") and SsisProjectFiles().matches("x/Project.params") and SsisProjectFiles().matches("x/W.conmgr")
    assert SsisProjectArchive().matches("d/p.ispac") and not SsisPackageSource().matches("a/b.xml")


def test_inventory_language():
    from repo_intel.extractors import detect_language

    assert detect_language(LOAD) == "ssis" and detect_language("x/W.conmgr") == "ssis" and detect_language(ISPAC) == "ssis"


def test_every_construct_the_pack_names_is_listed():
    ids = {c.id for c in PACK.constructs}
    used = set()
    for f in PKG.glob("*.py"):
        used |= set(re.findall(r'"(ssis:[a-z_]+(?:\.[a-z_]+)?)"', f.read_text()))
    used -= {t.name for t in PACK.terms} | {"ssis:" + n for n in ("ordersfile",)}
    assert used and used <= ids, sorted(used - ids)


def test_no_module_level_import_of_the_extractors_package():
    for f in PKG.glob("*.py"):
        for line in f.read_text().splitlines():
            assert not re.match(r"(from|import) repo_intel\.extractors", line), (f.name, line)


# ---------------------------------------------------------------------- the load package


def test_package_job_tasks_and_order():
    out = package()
    assert out.parsed
    ents, rels = entities(out), relations(out)
    attrs = ents[job()]
    assert attrs["platform_kind"] == "ssis:Package" and attrs["package_name"] == "LoadSales" and attrs["protection_level"] == "DontSaveSensitive"
    assert attrs["parameters"] == ["RunDate"] and attrs["variables"] == ["User::HighValueRows"]
    for name, kind in (("Check inbox", "script"), ("Load", "sequence"), ("Load/Truncate staging", "execute_sql"), ("Load/Load orders", "data_flow"),
                       ("Summarise", "data_flow"), ("Archive inbox", "file_system"), ("Publish marts", "execute_package"), ("Notify failure", "send_mail")):
        assert ents[task(name)]["type"] == kind, name
        assert (job(), "CONTAINS", task(name)) in rels
    assert ents[task("Load/Truncate staging")]["container"] == "Load"
    assert rels[(task("Load"), "DEPENDS_ON", task("Check inbox"))] == {"via": "precedence", "on": "success"}
    assert rels[(task("Archive inbox"), "DEPENDS_ON", task("Summarise"))]["condition"] == "expression_and_constraint"
    assert rels[(task("Notify failure"), "DEPENDS_ON", task("Load"))] == {"via": "precedence", "on": "failure", "logical_and": False}
    assert rels[(task("Publish marts"), "RUNS", job(PUBLISH))] == {"via": "execute_package"}
    check_contract(out, admitted(layer_bytes(LOAD)))


def test_connections_layouts_and_parameters_by_name():
    out = package()
    ents, rels = entities(out), relations(out)
    assert ents[cfg("connection:OrdersFile")] == {"creation_name": "FLATFILE", "scope": "package", "expression": True, "platform": "ssis", "platform_kind": "ssis:ConnectionManager", "fidelity": "lossy"}
    layout = key("DataSchema", kind="record_layout", name="ssis:OrdersFile")
    assert ents[layout]["columns"] == 5
    assert ents[key("Field", table="ssis:OrdersFile", name="Amount")]["position"] == 4
    assert (layout, "CONTAINS", key("Field", table="ssis:OrdersFile", name="OrderID")) in rels
    assert ents[cfg(f"parameter:{LOAD}#$Package::RunDate")]["scope"] == "package"
    assert (prog(f"{LOAD}#Load/Truncate staging"), "REFERENCES_CONFIG", cfg(f"parameter:{LOAD}#$Package::RunDate")) in rels


def test_execute_sql_is_a_sql_step_with_bound_parameters():
    out = package()
    unit = unit_of(out, "behavior", f"{LOAD}#Load/Truncate staging")
    assert unit["kind"] == "sql_step" and unit["language"] == "ssis"
    truncate, delete = unit["body"]
    assert truncate["sql"] == "TRUNCATE TABLE stage.orders" and truncate["params"] == []
    assert delete["sql"].startswith("DELETE FROM stage.orders_high") and delete["params"] == [{"name": "0", "value": {"e": "var", "scope": "package", "name": "RunDate"}}]
    assert "datasource" not in delete  # the project connection is known only by its id here
    rels = relations(out)
    for t in ("stage.orders", "stage.orders_high"):
        assert (prog(f"{LOAD}#Load/Truncate staging"), "WRITES_TO", table(t)) in rels
        assert (task("Load/Truncate staging"), "WRITES_TO", table(t)) not in rels, "the Task RUNS the Program, which carries the lineage"
    assert any("known only by its id" in m for m in messages(out))


def test_data_flow_transform_shapes():
    out = package()
    shape = unit_of(out, "transform", f"{LOAD}#Load/Load orders")
    assert shape["language"] == "ssis"
    ops = [(t["op"], t.get("primitive")) for t in shape["transforms"]]
    assert ("join", "join") in ops and ("project", "trim_strings") in ops and ("project", "normalize_case") in ops
    joins = [t for t in shape["transforms"] if t["op"] == "join"]
    assert [j["join"]["type"] for j in joins] == ["inner", "anti"]
    assert joins[0]["join"]["on"] == [["customerid", "customerid"]] and joins[0]["columns"] == {"add": ["region"]}
    splits = [t for t in shape["transforms"] if t.get("primitive") == "split_by_condition"]
    assert [s["output"] for s in splits] == ["stage.orders_high", "stage.orders"]
    assert splits[1]["predicate"] == {"e": "unop", "op": "not", "operand": splits[0]["predicate"]}
    summary = unit_of(out, "transform", f"{LOAD}#Summarise")
    agg = next(t for t in summary["transforms"] if t["op"] == "aggregate")
    assert agg["keys"] == ["region"] and agg["primitive"] == "aggregate"
    assert agg["measures"] == [{"fn": "sum", "as": "totalamount", "column": "netamount"}, {"fn": "count_distinct", "as": "orders", "column": "orderid"}]
    sort = summary["transforms"][-1]
    assert sort["op"] == "sort" and sort["order"] == [{"column": "totalamount", "desc": True}] and sort["output"] == "dw.sales_by_region"
    rels = relations(out)
    assert (prog(f"{LOAD}#Summarise"), "WRITES_TO", table("dw.sales_by_region")) in rels
    lineage = [(s, p) for (s, p, o) in rels if p in ("READS_FROM", "WRITES_TO")]
    assert lineage and all(s[0] == "Program" for s, _ in lineage), "a data flow's Task RUNS its Program; only the Program carries lineage"
    assert any(s == task("Summarise") and p == "REFERENCES_CONFIG" for (s, p, o) in rels), "the Task still names the connections it uses"


def test_data_flow_flow_unit_lowers_derived_columns_and_the_split():
    out = package()
    unit = unit_of(out, "behavior", f"{LOAD}#Load/Load orders")
    assert unit["kind"] == "flow"
    (loop,) = unit["body"]
    assert loop["op"] == "loop" and loop["kind"] == "implicit"
    assigns = {s["target"]["name"]: s["value"] for s in loop["body"] if s["op"] == "assign"}
    assert set(assigns) == {"CustomerName", "Region", "NetAmount", "IsPriority"}
    assert assigns["Region"] == {"e": "call", "callee": {"e": "name", "name": "UPPER", "kind": "builtin"}, "args": [{"e": "var", "scope": "column", "name": "Region"}]}
    split = next(s for s in loop["body"] if s["op"] == "if")
    assert split["then"][0]["value"]["value"] == "High value"
    assert {s["construct"] for s in loop["body"] if s["op"] == "platform"} >= {"ssis:dataflow.lookup", "ssis:dataflow.destination", "ssis:dataflow.row_count"}


def test_script_and_other_tasks_are_counted_not_modelled():
    counts = constructs(package())
    for cid in ("ssis:task.script", "ssis:task.file_system", "ssis:task.send_mail", "ssis:event_handler", "ssis:dataflow.row_count", "ssis:connection_expression"):
        assert counts.get((cid, False)), cid
    assert not any(k[0] == "Program" and "Check inbox" in k[1] for k in entities(package()))


def test_dynamic_sql_and_destination_stay_dynamic():
    out = package(PUBLISH)
    ents = entities(out)
    assert prog(f"{PUBLISH}#Rebuild indexes") not in ents
    assert "Execute SQL task Rebuild indexes of etl/SalesLoad/Publish.dtsx takes its SQL from a variable: not read" in messages(out)
    shape = unit_of(out, "transform", f"{PUBLISH}#Copy to mart")
    (read,) = shape["transforms"]
    assert read["inputs"] == ["dw.sales_by_region"] and read["output"] == "{user.marttable}" and read["dynamic"] is True
    assert not any(k[0] == "Table" and "mart" in k[1] and "history" not in k[1] for k in ents)
    rels = relations(out)
    assert rels[(task("Rebuild indexes", PUBLISH), "DEPENDS_ON", task("Copy to mart", PUBLISH))]["on"] == "completion"
    check_contract(out, admitted(layer_bytes(PUBLISH)))


def test_names_never_values():
    outs = [package(), package(PUBLISH), run(SsisProjectFiles(), "etl/SalesLoad/Warehouse.conmgr", layer_bytes("etl/SalesLoad/Warehouse.conmgr")),
            run(SsisProjectFiles(), "etl/SalesLoad/Project.params", layer_bytes("etl/SalesLoad/Project.params")), archive()]
    for out in outs:
        text = blob(out)
        for value in NEVER:
            assert value not in text, value


# ---------------------------------------------------------------------- project files


def test_project_connection_and_parameters():
    out = run(SsisProjectFiles(), "etl/SalesLoad/Warehouse.conmgr", layer_bytes("etl/SalesLoad/Warehouse.conmgr"))
    assert out.parsed
    assert entities(out)[cfg("connection:Warehouse")] == {"creation_name": "OLEDB", "scope": "project", "platform": "ssis", "platform_kind": "ssis:ConnectionManager", "fidelity": "lossy"}
    out = run(SsisProjectFiles(), "etl/SalesLoad/Project.params", layer_bytes("etl/SalesLoad/Project.params"))
    ents = entities(out)
    assert ents[cfg("parameter:etl/SalesLoad#$Project::InboxPath")]["required"] is True
    assert ents[cfg("parameter:etl/SalesLoad#$Project::BatchSize")]["data_type"] == "9"


def test_other_params_files_are_left_alone():
    out = run(SsisProjectFiles(), "conf/app.params", b"timeout=30\nretries=2\n")
    assert not out.parsed and out.observations == [] and out.diagnostics == []


def test_the_project_resolves_across_files_when_compiled():
    out = package(path="etl/SalesLoad/LoadReturns.dtsx", layer="commits/01-returns-package")
    rels = relations(out)
    ref_ = (prog("etl/SalesLoad/LoadReturns.dtsx#Mark returned orders"), "REFERENCES_CONFIG", cfg("parameter:etl/SalesLoad#$Project::BatchSize"))
    assert ref_ in rels  # the same key Project.params declares
    shape = unit_of(out, "transform", "etl/SalesLoad/LoadReturns.dtsx#Load returns")
    join = next(t for t in shape["transforms"] if t["op"] == "join")
    assert join["join"] == {"type": "left", "on": [["orderid", "orderid"]]} and join["primitive"] == "join"
    dedupe = next(t for t in shape["transforms"] if t["op"] == "deduplicate")
    assert dedupe["keys"] == ["orderid"] and dedupe["primitive"] == "dedupe_by_key"


# ---------------------------------------------------------------------- the deployment archive


def test_archive_packages_entry_points_and_connections():
    out = archive()
    assert out.parsed
    ents, rels = entities(out), relations(out)
    lj = f"{ISPAC}#LoadSales.dtsx"
    assert ents[job(lj)]["entry_point"] is True and ents[job(lj)]["archive"] == ISPAC
    assert "entry_point" not in ents[job(f"{ISPAC}#Publish.dtsx")]
    # Inside the archive the project connection is known, so the SQL task is linked to it.
    assert (prog(f"{lj}#Load/Truncate staging"), "REFERENCES_CONFIG", cfg("connection:Warehouse")) in rels
    unit = unit_of(out, "behavior", f"{lj}#Load/Truncate staging")
    assert {n["datasource"] for n in unit["body"]} == {"Warehouse"}
    assert (task("Publish marts", lj), "RUNS", job(f"{ISPAC}#Publish.dtsx")) in rels
    assert ents[cfg(f"parameter:{ISPAC}#$Project::InboxPath")]["scope"] == "project"
    assert constructs(out).get(("ssis:project_manifest", True)) == 1
    check_contract(out, admitted(layer_bytes(ISPAC, "commits/02-deployment-archive")))


def test_archive_member_secrets_are_masked_when_read():
    data = zip_of({"Warehouse.conmgr": (layer_bytes("etl/SalesLoad/Warehouse.conmgr"), zipfile.ZIP_DEFLATED)})
    assert b"fixture-not-a-real-password" not in data  # compressed: admission cannot see it
    z = xmlread.ZipReader(data)
    assert z.open() is None
    member = z.read("Warehouse.conmgr")
    assert member is not None and b"fixture-not-a-real-password" not in member.data and b"Warehouse" in member.data
    assert not member.place.exact


def test_stored_members_keep_exact_spans_after_admission():
    data = layer_bytes(ISPAC, "commits/02-deployment-archive")
    masked = admitted(data)
    assert len(masked) == len(data) and b"fixture-not-a-real-password" not in masked
    out = archive()
    warehouse = next(o for o in out.observations if o.kind == "entity" and o.payload["natural_key"] == {"name": "connection:Warehouse"})
    s = warehouse.evidence.span
    assert b"Warehouse" in masked[s.start_byte:s.end_byte] and s.end_byte - s.start_byte < 1000


def test_the_committed_archive_is_what_the_script_writes():
    spec = importlib.util.spec_from_file_location("make_ispac", FIXTURE / "make_ispac.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build() == layer_bytes(ISPAC, "commits/02-deployment-archive")


def zip_of(members: dict[str, tuple[bytes, int]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, (data, method) in sorted(members.items()):
            info = zipfile.ZipInfo(name, DATE)
            info.compress_type = method
            zf.writestr(info, data)
    return buf.getvalue()


def test_archive_bounds(monkeypatch):
    pkg = dtsx(sql_task("Run", "DELETE FROM a.b"), OLEDB)
    data = zip_of({"A.dtsx": (pkg, zipfile.ZIP_DEFLATED), "B.dtsx": (pkg, zipfile.ZIP_STORED)})
    monkeypatch.setattr(xmlread, "MAX_ENTRIES", 1)
    out = run(SsisProjectArchive(), ISPAC, data)
    assert not out.parsed and messages(out) == ["archive has more than 1 members: not read"]
    monkeypatch.setattr(xmlread, "MAX_ENTRIES", 2000)
    monkeypatch.setattr(xmlread, "MAX_MEMBER", len(pkg) - 1)
    out = run(SsisProjectArchive(), ISPAC, data)
    assert sum("is larger than" in m for m in messages(out)) == 2
    assert not any(k[0] == "Job" for k in entities(out))
    monkeypatch.setattr(xmlread, "MAX_MEMBER", 4_000_000)
    monkeypatch.setattr(xmlread, "MAX_TOTAL", len(pkg) + 1)
    out = run(SsisProjectArchive(), ISPAC, data)
    assert len([k for k in entities(out) if k[0] == "Job"]) == 1
    assert any("more than" in m and "bytes of members" in m for m in messages(out))


def test_not_a_zip_and_encrypted_members():
    out = run(SsisProjectArchive(), ISPAC, b"not an archive")
    assert not out.parsed and messages(out) == ["not a zip archive"]
    data = bytearray(zip_of({"A.dtsx": (dtsx(""), zipfile.ZIP_STORED)}))
    data[6] |= 0x1  # the local header's encryption flag
    cd = data.rfind(b"PK\x01\x02")
    data[cd + 8] |= 0x1
    out = run(SsisProjectArchive(), ISPAC, bytes(data))
    assert "member A.dtsx is encrypted: not read" in messages(out)


# ---------------------------------------------------------------------- refusals and small cases


def test_doctype_and_entities_are_refused():
    data = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:ExecutableType="Microsoft.Package">&a;</DTS:Executable>'
    out = package(path="x/E.dtsx", data=data)
    assert not out.parsed and out.observations == []
    assert messages(out) == ["x/E.dtsx: XML with an internal DTD or entity declarations is not read"]


def test_malformed_and_foreign_xml():
    out = package(path="x/M.dtsx", data=b"<DTS:Executable><unclosed>")
    assert not out.parsed and any("not well-formed XML" in m for m in messages(out))
    out = package(path="x/F.dtsx", data=b"<root/>")
    assert not out.parsed and any("not an SSIS package" in m for m in messages(out))


def test_package_connection_resolves_sql_task():
    out = package(path="x/T.dtsx", data=dtsx(sql_task("Run", "INSERT INTO dw.a SELECT * FROM st.b"), OLEDB))
    rels = relations(out)
    assert (prog("x/T.dtsx#Run"), "REFERENCES_CONFIG", cfg("connection:DB")) in rels
    assert (prog("x/T.dtsx#Run"), "WRITES_TO", table("dw.a")) in rels and (prog("x/T.dtsx#Run"), "READS_FROM", table("st.b")) in rels
    assert unit_of(out, "behavior", "x/T.dtsx#Run")["body"][0]["datasource"] == "DB"
    assert "db01" not in blob(out) and "fixture-not-a-real-password" not in blob(out)


@pytest.mark.parametrize("source_type,why", [("Variable", "a variable"), ("FileConnection", "a file connection")])
def test_sql_from_elsewhere_is_not_read(source_type, why):
    out = package(path="x/T.dtsx", data=dtsx(sql_task("Run", "User::Q", source_type=source_type), OLEDB))
    assert f"Execute SQL task Run of x/T.dtsx takes its SQL from {why}: not read" in messages(out)
    assert constructs(out).get(("ssis:task.execute_sql_dynamic", False)) == 1
    assert (task("Run", "x/T.dtsx"), "REFERENCES_CONFIG", cfg("connection:DB")) in relations(out)


def test_execute_package_by_file_connection_is_not_linked():
    ex = ('<DTS:Executable DTS:refId="Package\\Child" DTS:ExecutableType="Microsoft.ExecutePackageTask" DTS:ObjectName="Child"><DTS:ObjectData>'
          "<ExecutePackageTask><UseProjectReference>False</UseProjectReference><Connection>Other</Connection></ExecutePackageTask></DTS:ObjectData></DTS:Executable>")
    out = package(path="x/T.dtsx", data=dtsx(ex))
    assert constructs(out).get(("ssis:task.execute_package_external", False)) == 1
    assert not any(p == "RUNS" for _, p, _ in relations(out))


def test_2008_property_form():
    data = ('<?xml version="1.0"?><DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:ExecutableType="MSDTS.Package.1">'
            '<DTS:Property DTS:Name="ObjectName">Old</DTS:Property><DTS:Executable DTS:ExecutableType="Microsoft.SqlServer.Dts.Tasks.ExecuteSQLTask.ExecuteSQLTask, Microsoft.SqlServer.SQLTask">'
            '<DTS:Property DTS:Name="ObjectName">Run</DTS:Property><DTS:ObjectData><SQLTask:SqlTaskData xmlns:SQLTask="www.microsoft.com/sqlserver/dts/tasks/sqltask" SQLTask:SqlStatementSource="DELETE FROM a.b" />'
            "</DTS:ObjectData></DTS:Executable></DTS:Executable>").encode()
    out = package(path="x/Old.dtsx", data=data)
    ents = entities(out)
    assert ents[job("x/Old.dtsx")]["package_name"] == "Old"
    assert ents[task("Run", "x/Old.dtsx")]["type"] == "execute_sql"


def test_output_is_deterministic():
    for make in (package, archive):
        a, b = make(), make()
        assert [json.dumps(o.payload, sort_keys=True, default=str) for o in a.observations] == [json.dumps(o.payload, sort_keys=True, default=str) for o in b.observations]
        assert messages(a) == messages(b)
