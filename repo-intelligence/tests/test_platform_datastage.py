"""IBM DataStage exports (repo_intel/platforms/datastage): `.dsx` files in the text and XML
forms and `.isx` archives read into Jobs, stage Tasks in link order, the files and tables
they read and write, transform shapes per job and behaviour for transformers and sequences."""

import hashlib
import importlib.util
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import validate_unit
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.compile.transforms import validate_shape
from repo_intel.platforms import packs, validate_pack
from repo_intel.platforms.datastage import PACK, dsx, expr, source, vocabulary, xmlspan
from repo_intel.platforms.datastage.source import DsxExport, IsxArchive

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "F32-datastage-job"
CUSTOMERS = "datastage/export/LoadCustomers.dsx"
NIGHTLY = "datastage/export/NightlyLoad.dsx"
ISX = "datastage/isx/orders.isx"
PRODUCTS = "datastage/export/LoadProducts.dsx"


def fixture(rel: str, layer: str = "base") -> bytes:
    return (FIXTURE / layer / rel).read_bytes()


def run(data: bytes, path: str = "jobs/Sample.dsx"):
    extractor = IsxArchive() if path.lower().endswith(".isx") else DsxExport()
    return extractor.extract(path, data, hashlib.sha256(data).hexdigest())


def ref(x) -> tuple:
    return x if isinstance(x, str) else (x["kind"], json.dumps(x["natural_key"], sort_keys=True))


def key(entity_kind: str, **nk) -> tuple:
    return (entity_kind, json.dumps(nk, sort_keys=True))


def job(name: str) -> tuple:
    return key("Job", framework="datastage", name=name)


def task(j: str, name: str) -> tuple:
    return key("Task", framework="datastage", job=j, name=name)


def table(name: str) -> tuple:
    return key("Table", datastore="default", name=name)


def dataset(name: str) -> tuple:
    return key("Dataset", name=name)


def entities(out) -> dict[tuple, dict]:
    found: dict[tuple, dict] = {}
    for o in out.observations:
        if o.kind == "entity":
            found.setdefault(ref(o.payload), {}).update(o.payload["attributes"])
    return found


def relations(out) -> set[tuple]:
    return {(ref(o.payload["subject"]), o.payload["predicate"], ref(o.payload["object"])) for o in out.observations if o.kind == "relation"}


def qualifiers(out, subject, predicate, obj) -> dict:
    for o in out.observations:
        p = o.payload
        if o.kind == "relation" and (ref(p["subject"]), p["predicate"], ref(p["object"])) == (subject, predicate, obj):
            return p.get("qualifiers") or {}
    raise AssertionError("no such relation")


def constructs(out) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = {}
    for o in out.observations:
        if o.kind == "construct":
            k = (o.payload["construct"], o.payload["modelled"])
            counts[k] = counts.get(k, 0) + 1
    return counts


def units(out, kind: str) -> list[dict]:
    return [o.payload for o in out.observations if o.kind == kind]


def transforms(out, job_name: str) -> list[dict]:
    found = [u for u in units(out, "transform") if u["owner"]["natural_key"]["name"] == job_name]
    assert len(found) == 1
    return found[0]["transforms"]


def messages(out) -> list[str]:
    return [d.message for d in out.diagnostics]


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def check_all_valid(out) -> None:
    for u in units(out, "behavior"):
        assert validate_unit(u) == [], u
    for u in units(out, "transform"):
        assert validate_shape(u) == [], u
    for o in out.observations:
        p = o.payload
        if o.kind == "relation" and isinstance(p["subject"], dict) and isinstance(p["object"], dict):
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p


# ---------------------------------------------------------------------- writing small exports


def esc(value: str) -> str:
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    return re.sub(r"[\x00-\x1f]", lambda m: f"\\({ord(m.group()):x})", out)


def px(name: str, value: str) -> str:
    """A parallel stage property value as the Designer writes it."""
    return f"\x02\x01\x03{name}\x02{value}\x01"


def rec(ident: str, ole: str, props: dict, **collections) -> str:
    lines = ["   BEGIN DSRECORD", f'      Identifier "{ident}"', f'      OLEType "{ole}"']
    for k, v in props.items():
        lines.append(f'      {k} "{esc(v)}"')
    for cname, rows in collections.items():
        lines.append(f'      {cname} "CSub"')
        for row in rows:
            lines.append("      BEGIN DSSUBRECORD")
            for k, v in row.items():
                lines.append(f'         {k} "{esc(v)}"')
            lines.append("      END DSSUBRECORD")
    lines.append("   END DSRECORD")
    return "\n".join(lines)


def export(name: str, job_type: str, *records: str, extra: str = "") -> bytes:
    head = 'BEGIN HEADER\n   CharacterSet "CP1252"\n   ServerName "SECRETHOST9"\nEND HEADER\n'
    root = rec("ROOT", "CJobDefn", {"Name": name, "JobType": job_type})
    return (head + f'BEGIN DSJOB\n   Identifier "{name}"\n' + "\n".join((root,) + records) + "\nEND DSJOB\n" + extra).encode()


def stage(ident: str, name: str, stype: str, ins: str = "", outs: str = "", ole: str = "CCustomStage", props: dict | None = None, **collections) -> str:
    p = {"Name": name, "StageType": stype}
    if ins:
        p["InputPins"] = ins
    if outs:
        p["OutputPins"] = outs
    p.update(props or {})
    return rec(ident, ole, p, **collections)


def pin(ident: str, name: str, partner: str, output: bool, props: dict | None = None, **collections) -> str:
    return rec(ident, "CCustomOutput" if output else "CCustomInput", {"Name": name, "Partner": partner, **(props or {})}, **collections)


def chain(*middle: tuple[str, str, dict]) -> list[str]:
    """A file source, the given single-input single-output stages, and a file target."""
    n = len(middle) + 2
    records = [stage("V0S0", "src", "PxSequentialFile", outs="V0S0P1"), pin("V0S0P1", "l0", "V0S1|V0S1P1", True, Properties=[{"Name": "file", "Value": px("file", "/in/a.txt")}])]
    for i, (name, stype, colls) in enumerate(middle, start=1):
        records.append(stage(f"V0S{i}", name, stype, ins=f"V0S{i}P1", outs=f"V0S{i}P2", **colls))
        records.append(pin(f"V0S{i}P1", f"l{i - 1}", f"V0S{i - 1}|V0S{i - 1}P{1 if i == 1 else 2}", False))
        records.append(pin(f"V0S{i}P2", f"l{i}", f"V0S{i + 1}|V0S{i + 1}P1", True))
    last = n - 1
    records.append(stage(f"V0S{last}", "tgt", "PxSequentialFile", ins=f"V0S{last}P1"))
    records.append(pin(f"V0S{last}P1", f"l{last - 1}", f"V0S{last - 1}|V0S{last - 1}P{1 if last == 1 else 2}", False, Properties=[{"Name": "file", "Value": px("file", "/out/b.txt")}]))
    return records


# ---------------------------------------------------------------------- registration


def test_registered_as_the_datastage_pack():
    assert packs()["datastage"] is PACK
    assert validate_pack(PACK) == []
    assert {e.id for e in PACK.extractors} == {"datastage.dsx", "datastage.isx"}
    assert DsxExport().matches("a/B.DSX") and IsxArchive().matches("x/y.isx") and not DsxExport().matches("a/b.xml")
    terms = {t.name for t in PACK.terms}
    assert {"ds:ParallelJob", "ds:ServerJob", "ds:SequenceJob", "ds:MainframeJob", "ds:Stage", "ds:Activity", "ds:Routine", "ds:TableDefinition", "ds:Column"} <= terms


def test_inventory_language_of_exports_is_datastage():
    from repo_intel.extractors import detect_language

    assert detect_language(CUSTOMERS) == "datastage" and detect_language(ISX) == "datastage"


def test_every_construct_and_term_the_reader_names_is_listed():
    ids = {c.id for c in PACK.constructs}
    terms = {t.name for t in PACK.terms}
    pkg = Path(source.__file__).parent
    used = set()
    for name in ("jobs.py", "source.py", "expr.py"):
        used |= set(re.findall(r'"(ds:[A-Za-z_][A-Za-z0-9_.]*)"', (pkg / name).read_text()))
    assert used, "the reader names constructs"
    missing = {u for u in used if u not in ids and u not in terms}
    assert not missing, sorted(missing)
    for canonical, *_ in vocabulary._STAGES:
        assert f"ds:stage.{canonical}" in ids
    assert "ds:stage.unknown" in ids
    for c in PACK.constructs:
        assert c.modelled_as.split(":")[0] in ("entity", "relation", "behavior", "attribute", "unmapped"), c


def test_stage_kind_takes_the_activity_names_with_or_without_the_prefix():
    assert vocabulary.stage_kind("CJSJobActivity")[1] == "job_activity"
    assert vocabulary.stage_kind("JSJobActivity")[1] == "job_activity"
    assert vocabulary.stage_kind("pxsort")[0] == "PxSort"
    assert vocabulary.stage_kind("NoSuchStage") is None


# ---------------------------------------------------------------------- the fixture: a parallel job


def test_parallel_job_stages_links_and_data():
    out = run(fixture(CUSTOMERS), CUSTOMERS)
    assert out.parsed
    ents, rels = entities(out), relations(out)
    j = ents[job("LoadCustomers")]
    assert j["platform_kind"] == "ds:ParallelJob" and j["job_type"] == "parallel" and j["category"] == "Jobs/Customers"
    assert j["parameters"] == ["DW_PASSWORD", "REJECT_DIR"]
    names = ("src_customers", "ref_country", "lkp_country", "xfm_clean", "srt_cust", "rmd_cust", "tgt_customers", "tgt_rejects")
    for n in names:
        assert (job("LoadCustomers"), "CONTAINS", task("LoadCustomers", n)) in rels
    assert ents[task("LoadCustomers", "xfm_clean")]["stage_type"] == "CTransformerStage"
    assert ents[task("LoadCustomers", "ref_country")]["dialect"] == "oracle"
    for down, up in (("lkp_country", "src_customers"), ("lkp_country", "ref_country"), ("xfm_clean", "lkp_country"), ("srt_cust", "xfm_clean"),
                     ("rmd_cust", "srt_cust"), ("tgt_customers", "rmd_cust"), ("tgt_rejects", "xfm_clean")):
        assert (task("LoadCustomers", down), "DEPENDS_ON", task("LoadCustomers", up)) in rels
    q = qualifiers(out, task("LoadCustomers", "tgt_rejects"), "DEPENDS_ON", task("LoadCustomers", "xfm_clean"))
    assert q == {"via": "link", "link": "lnk_reject"}
    assert (task("LoadCustomers", "src_customers"), "READS_FROM", dataset("/data/landing/customers.txt")) in rels
    assert (task("LoadCustomers", "ref_country"), "READS_FROM", table("dw.country")) in rels
    assert (task("LoadCustomers", "tgt_customers"), "WRITES_TO", table("dw.customers")) in rels
    check_all_valid(out)


def test_transform_shapes_follow_the_links():
    out = run(fixture(CUSTOMERS), CUSTOMERS)
    ts = transforms(out, "LoadCustomers")
    by_id = {t["id"]: t for t in ts}
    assert ts[0]["op"] == "read" and ts[0]["inputs"] == ["/data/landing/customers.txt"]
    join = next(t for t in ts if t["op"] == "join")
    assert join["primitive"] == "join" and join["join"]["type"] == "left" and join["keys"] == ["COUNTRY_CODE"]
    filters = [t for t in ts if t["op"] == "filter" and t.get("primitive") == "split_by_condition"]
    assert len(filters) == 2 and all(f["inputs"] == [join["id"]] for f in filters)
    prims = {t.get("primitive") for t in ts}
    assert {"trim_strings", "normalize_case", "apply_defaults", "parse_timestamp", "conditional_flag", "dedupe_by_key", "merge_into"} <= prims
    sort = next(t for t in ts if t["op"] == "sort")
    assert sort["order"] == [{"column": "CUST_ID", "desc": False}]
    publish = next(t for t in ts if t["op"] == "publish" and t["output"] == "dw.customers")
    assert publish["primitive"] == "merge_into" and by_id[publish["inputs"][0]]["op"] == "deduplicate"
    rejects = next(t for t in ts if t["op"] == "publish" and t is not publish)
    assert rejects["output"] == "{REJECT_DIR}/customers_rejects.txt" and rejects.get("dynamic") is True


def test_transformer_behaviour_is_a_row_loop_with_guarded_outputs():
    data = fixture(CUSTOMERS)
    out = run(data, CUSTOMERS)
    flows = [u for u in units(out, "behavior") if u["owner"]["kind"] == "Task"]
    assert len(flows) == 1
    unit = flows[0]
    assert unit["owner"]["natural_key"]["name"] == "xfm_clean" and unit["language"] == "datastage" and unit["kind"] == "flow"
    loop = unit["body"][0]
    assert loop["op"] == "loop" and loop["kind"] == "implicit" and loop["over"] == {"e": "var", "scope": "link", "name": "lnk_enriched"}
    ops = [s["op"] for s in loop["body"]]
    assert ops[0] == "assign" and loop["body"][0]["target"] == {"e": "var", "scope": "stage", "name": "svIsActive"}
    guards = [s for s in loop["body"] if s["op"] == "if"]
    assert len(guards) == 2
    outputs = {s["name"] for g in guards for s in g["then"] if s["op"] == "output"}
    assert outputs == {"lnk_valid", "lnk_reject"}
    calls = {n["callee"]["name"] for n in walk(unit["body"]) if n.get("e") == "call"}
    assert {"Trim", "UpCase", "NullToValue", "StringToDate"} <= calls
    a, b = loop["at"]
    assert b"xfm_clean" in data[a:b]
    check_all_valid(out)


def test_table_definitions_and_routines():
    out = run(fixture(CUSTOMERS), CUSTOMERS)
    ents, rels = entities(out), relations(out)
    layout = "datastage:Database/DWH/DW.CUSTOMERS"
    schema = key("DataSchema", kind="table_definition", name=layout)
    assert ents[schema]["platform_kind"] == "ds:TableDefinition" and ents[schema]["table_name"] == "DW.CUSTOMERS"
    cust_id = key("Field", table=layout, name="CUST_ID")
    assert (schema, "CONTAINS", cust_id) in rels
    assert ents[cust_id] | {"type": "integer", "key": True, "nullable": False, "position": 1} == ents[cust_id]
    assert ents[key("Field", table=layout, name="SIGNUP_DATE")]["type"] == "date"
    routine = key("Program", platform="datastage", name="CheckLandingFile")
    assert ents[routine]["platform_kind"] == "ds:Routine"
    counts = constructs(out)
    assert counts[("ds:routine_source", False)] == 1 and counts[("ds:table_definition", True)] == 1


def test_a_name_built_from_parameters_stays_dynamic():
    out = run(fixture(CUSTOMERS), CUSTOMERS)
    ents = entities(out)
    assert not any(k[0] == "Dataset" and ("REJECT_DIR" in k[1] or "customers_rejects" in k[1]) for k in ents)
    assert constructs(out)[("ds:dynamic_name", False)] == 1
    assert "stage tgt_rejects of LoadCustomers writes a file named by job parameters: not resolved" in messages(out)


def test_parameter_defaults_server_names_and_addresses_are_never_copied():
    for rel in (CUSTOMERS, NIGHTLY, ISX):
        out = run(fixture(rel), rel)
        blob = json.dumps([o.payload for o in out.observations], default=str) + repr(messages(out))
        for probe in ("not-a-real-default-0000", "etl_reader", "etl@example.invalid", "dwh-support@example.invalid", "ETLHOST01", "2024-01-01", "DW_SERVER"):
            assert probe not in blob, (rel, probe)
    assert b"not-a-real-default-0000" in fixture(CUSTOMERS)  # the value is there to be left out


# ---------------------------------------------------------------------- the fixture: a sequence


def test_sequence_activities_triggers_and_what_they_run():
    out = run(fixture(NIGHTLY), NIGHTLY)
    ents, rels = entities(out), relations(out)
    j = ents[job("NightlyLoad")]
    assert j["platform_kind"] == "ds:SequenceJob" and j["job_type"] == "sequence"
    for n in ("chk_landing", "run_customers", "run_orders", "notify_support", "stop_sequence"):
        assert ents[task("NightlyLoad", n)]["platform_kind"] == "ds:Activity"
    assert (task("NightlyLoad", "run_customers"), "RUNS", job("LoadCustomers")) in rels
    assert (task("NightlyLoad", "run_orders"), "RUNS", job("LoadOrders")) in rels
    assert (task("NightlyLoad", "chk_landing"), "RUNS", key("Program", platform="datastage", name="CheckLandingFile")) in rels
    assert job("LoadCustomers") not in ents  # declared where the repository defines it, not here
    q = qualifiers(out, task("NightlyLoad", "run_orders"), "DEPENDS_ON", task("NightlyLoad", "run_customers"))
    assert q == {"via": "trigger", "link": "customers_ok", "condition_type": 1}
    assert not units(out, "transform")
    check_all_valid(out)


def test_sequence_behaviour_calls_jobs_under_their_triggers():
    out = run(fixture(NIGHTLY), NIGHTLY)
    (unit,) = units(out, "behavior")
    assert unit["owner"]["kind"] == "Job" and unit["kind"] == "flow"
    body = unit["body"]
    assert body[0]["op"] == "call" and body[0]["call"]["callee"] == {"e": "name", "name": "CheckLandingFile", "kind": "routine"}
    guarded = body[1]
    assert guarded["op"] == "if" and guarded["then"][0]["call"]["callee"]["name"] == "LoadCustomers"
    cond = guarded["cond"]
    assert cond["e"] == "binop" and cond["op"] == "==" and cond["left"]["object"] == {"e": "var", "scope": "activity", "name": "chk_landing"}
    # notify_support starts on a custom trigger or on a failure: either one
    notify = next(s for s in body if s["op"] == "if" and s["then"][0].get("construct") == "ds:stage.CJSMailActivity")
    assert notify["cond"]["op"] == "or" and {"e": "platform", "construct": "ds:link", "link": "customers_failed", "condition_type": 2} in list(walk(notify["cond"]))
    assert body[2]["op"] == "call" and body[2]["call"]["callee"]["name"] == "LoadOrders"  # an OK trigger guards nothing
    assert body[-1]["op"] == "exit"


# ---------------------------------------------------------------------- the fixture: an archive


def test_archive_member_in_the_xml_form():
    data = fixture(ISX)
    out = run(data, ISX)
    assert out.parsed
    ents, rels = entities(out), relations(out)
    assert ents[job("LoadOrders")]["parameters"] == ["RUN_DATE"]
    assert (task("LoadOrders", "src_orders"), "READS_FROM", table("sales.orders")) in rels
    assert (task("LoadOrders", "tgt_totals"), "WRITES_TO", dataset("/data/work/order_totals.ds")) in rels
    assert (task("LoadOrders", "agg_totals"), "DEPENDS_ON", task("LoadOrders", "src_orders")) in rels
    agg = next(t for t in transforms(out, "LoadOrders") if t["op"] == "aggregate")
    assert agg["keys"] == ["CUST_ID"]
    assert constructs(out)[("ds:isx_member", False)] == 1
    assert not any("not resolved" in m for m in messages(out))  # #RUN_DATE# sits inside a string literal
    check_all_valid(out)


def test_stored_member_spans_are_exact():
    data = fixture(ISX)
    out = run(data, ISX)
    for o in out.observations:
        s = o.evidence.span
        assert 0 <= s.start_byte <= s.end_byte <= len(data)
    src = next(o for o in out.observations if o.kind == "entity" and o.payload["natural_key"] == {"name": "sales.orders", "datastore": "default"})
    assert b"SALES.ORDERS" in data[src.evidence.span.start_byte:src.evidence.span.end_byte]


def test_layer_one_adds_the_filter():
    out = run(fixture(ISX, "commits/01-filter-orders"), ISX)
    rels = relations(out)
    assert (task("LoadOrders", "flt_open"), "DEPENDS_ON", task("LoadOrders", "src_orders")) in rels
    assert (task("LoadOrders", "agg_totals"), "DEPENDS_ON", task("LoadOrders", "flt_open")) in rels
    flt = next(t for t in transforms(out, "LoadOrders") if t["op"] == "filter" and "STATUS" in json.dumps(t.get("predicate")))
    assert flt["predicate"] == {"sql": "STATUS <> 'CANCELLED'"}


def test_product_job_modify_and_db2_target():
    out = run(fixture(PRODUCTS, "commits/02-product-job"), PRODUCTS)
    ents, rels = entities(out), relations(out)
    assert (task("LoadProducts", "tgt_products"), "WRITES_TO", table("dw.products")) in rels
    assert ents[task("LoadProducts", "tgt_products")]["dialect"] == "db2"
    ts = transforms(out, "LoadProducts")
    assert [t.get("primitive") for t in ts if t["op"] == "project"] == ["drop_columns", "rename_columns"]
    assert next(t for t in ts if t.get("primitive") == "rename_columns")["columns"] == {"rename": {"PROD_NM": "PRODUCT_NAME"}}


def test_the_committed_archives_are_what_the_script_writes(tmp_path):
    spec = importlib.util.spec_from_file_location("make_isx_f32", FIXTURE / "make_isx.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
        for i, layer in enumerate(mod.LAYERS):
            target = tmp_path / layer / mod.TARGET
            mod.write(target, i)
            assert target.read_bytes() == (FIXTURE / layer / mod.TARGET).read_bytes(), layer
    finally:
        sys.modules.pop(spec.name, None)


def test_output_is_deterministic():
    for rel in (CUSTOMERS, NIGHTLY, ISX):
        a, b = run(fixture(rel), rel), run(fixture(rel), rel)
        dump = lambda out: [json.dumps([o.kind, o.payload, o.evidence.span.start_byte, o.evidence.span.end_byte], sort_keys=True, default=str) for o in out.observations]
        assert dump(a) == dump(b) and messages(a) == messages(b)


# ---------------------------------------------------------------------- archives


def isx(members: dict[str, tuple[bytes, int]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, (body, method) in members.items():
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = method
            zf.writestr(info, body)
    return buf.getvalue()


def test_compressed_member_spans_are_the_member():
    body = export("Small", "3", *chain())
    data = isx({"Jobs/Small.pjb": (body, zipfile.ZIP_DEFLATED)})
    out = run(data, "a.isx")
    assert job("Small") in entities(out)
    spans = {(o.evidence.span.start_byte, o.evidence.span.end_byte) for o in out.observations if o.kind == "entity"}
    assert len(spans) == 1  # every entity cites the member's compressed bytes
    (a, b), = spans
    assert 0 < a < b <= len(data)


def test_text_export_member_and_meta_inf():
    data = isx({"META-INF/IS-MANIFEST.MF": (b"<manifest/>", zipfile.ZIP_STORED), "Jobs/Small.dsx": (export("Small", "3", *chain()), zipfile.ZIP_STORED)})
    out = run(data, "a.isx")
    assert job("Small") in entities(out) and ("ds:isx_member", False) not in constructs(out)


def test_not_a_zip_is_refused():
    out = run(b"PK\x03\x04 not really", "a.isx")
    assert not out.parsed and messages(out) == ["not a zip archive"]


def test_member_size_bound(monkeypatch):
    monkeypatch.setattr(source, "MAX_MEMBER", 100)
    out = run(isx({"Jobs/Small.pjb": (export("Small", "3", *chain()), zipfile.ZIP_DEFLATED)}), "a.isx")
    assert job("Small") not in entities(out)
    assert any("is larger than 100 bytes: not read" in m for m in messages(out))


def test_entry_count_bound(monkeypatch):
    monkeypatch.setattr(source, "MAX_ENTRIES", 1)
    out = run(isx({"a.txt": (b"x", zipfile.ZIP_STORED), "b.txt": (b"y", zipfile.ZIP_STORED)}), "a.isx")
    assert not out.parsed and "more than 1 members" in messages(out)[0]


def test_total_size_bound(monkeypatch):
    monkeypatch.setattr(source, "MAX_TOTAL", 10)
    out = run(isx({"a.txt": (b"x" * 8, zipfile.ZIP_STORED), "b.txt": (b"y" * 8, zipfile.ZIP_STORED)}), "a.isx")
    assert any("the rest is not read" in m for m in messages(out))
    assert constructs(out)[("ds:isx_member", False)] == 1


def test_encrypted_member_is_not_read():
    data = bytearray(isx({"Jobs/Small.dsx": (export("Small", "3", *chain()), zipfile.ZIP_STORED)}))
    # set the encrypted flag in the central directory entry
    cd = data.rfind(b"PK\x01\x02")
    data[cd + 8] |= 0x1
    out = run(bytes(data), "a.isx")
    assert any("is encrypted: not read" in m for m in messages(out))


# ---------------------------------------------------------------------- the XML form


XML_JOB = (
    '<?xml version="1.0" encoding="{enc}"?>\n<DSExport><Header CharacterSet="UTF-8"/>'
    '<Job Identifier="XJob"><Record Identifier="ROOT" Type="CJobDefn"><Property Name="Name">XJob</Property><Property Name="JobType">3</Property></Record>'
    '<Record Identifier="V0S0" Type="CCustomStage"><Property Name="Name">src</Property><Property Name="StageType">PxSequentialFile</Property><Property Name="OutputPins">V0S0P1</Property></Record>'
    '<Record Identifier="V0S0P1" Type="CCustomOutput"><Property Name="Name">l0</Property><Property Name="Partner">V0S1|V0S1P1</Property>'
    '<Collection Name="Properties" Type="CCustomProperty"><SubRecord><Property Name="Name">file</Property><Property Name="Value">\\(2)\\(1)\\(3)file\\(2)/in/x.txt\\(1)</Property></SubRecord></Collection></Record>'
    '<Record Identifier="V0S1" Type="CCustomStage"><Property Name="Name">tgt</Property><Property Name="StageType">PxDataSet</Property><Property Name="InputPins">V0S1P1</Property></Record>'
    '<Record Identifier="V0S1P1" Type="CCustomInput"><Property Name="Name">l0</Property><Property Name="Partner">V0S0|V0S0P1</Property>'
    '<Collection Name="Properties" Type="CCustomProperty"><SubRecord><Property Name="Name">dataset</Property><Property Name="Value">\\(2)\\(1)\\(3)dataset\\(2)/out/x.ds\\(1)</Property></SubRecord></Collection></Record>'
    '</Job></DSExport>\n'
)


def test_xml_export_file():
    data = XML_JOB.format(enc="UTF-8").encode()
    out = run(data, "x/XJob.dsx")
    rels = relations(out)
    assert (task("XJob", "src"), "READS_FROM", dataset("/in/x.txt")) in rels
    assert (task("XJob", "tgt"), "WRITES_TO", dataset("/out/x.ds")) in rels
    src = next(o for o in out.observations if o.kind == "entity" and o.payload["natural_key"] == {"name": "/in/x.txt"})
    assert b"/in/x.txt" in data[src.evidence.span.start_byte:src.evidence.span.end_byte]


def test_utf16_xml_export_cites_the_whole_file():
    data = XML_JOB.format(enc="UTF-16").encode("utf-16")
    out = run(data, "x/XJob.dsx")
    assert (task("XJob", "tgt"), "WRITES_TO", dataset("/out/x.ds")) in relations(out)
    assert {(o.evidence.span.start_byte, o.evidence.span.end_byte) for o in out.observations if o.kind == "entity"} == {(0, len(data))}


def test_doctype_and_entities_are_refused():
    data = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "boom">]><DSExport><Job Identifier="J"/></DSExport>'
    out = run(data, "x/J.dsx")
    assert not out.parsed and "not read" in messages(out)[0]
    with pytest.raises(xmlspan.XmlError):
        xmlspan.parse(data)


def test_not_an_export_is_refused():
    out = run(b"hello world\n", "x/J.dsx")
    assert not out.parsed and messages(out) == ["not read: not a DataStage export"]


def test_xmlspan_spans_of_empty_and_full_elements():
    data = b'<a><b x="1"/><c>text</c></a>'
    root, exact = xmlspan.parse(data)
    assert exact
    b, c = root.children
    assert data[b.start:b.end] == b'<b x="1"/>' and data[c.start:c.end] == b"<c>text</c>" and c.text == "text"


# ---------------------------------------------------------------------- the text form


def test_text_form_values_escapes_and_collections():
    data = (b'BEGIN DSJOB\n   Identifier "J"\n   BEGIN DSRECORD\n      Identifier "ROOT"\n      OLEType "CJobDefn"\n'
            b'      Name "J"\n      Description "say \\"hi\\" \\\\ there"\n      Parameters "CParameters"\n      BEGIN DSSUBRECORD\n         Name "P1"\n      END DSSUBRECORD\n'
            b'      JobControl =+=+=+=\nline one\nline two\n=+=+=+=\n   END DSRECORD\nEND DSJOB\n')
    ex = dsx.parse(data)
    (section,) = ex.sections
    (root,) = section.records
    assert root.get("Description") == 'say "hi" \\ there'
    assert [s["Name"].text for s in root.collection("Parameters")] == ["P1"]
    jc = root.value("JobControl")
    assert jc.text == "line one\nline two" and data[jc.start:jc.end].startswith(b"line one")
    out = run(data, "x/J.dsx")
    assert constructs(out)[("ds:job_control", False)] == 1
    assert dsx.unescape("a\\(41)b") == "aAb"


def test_text_form_with_utf8_characters_keeps_byte_spans():
    records = chain()
    records[1] = records[1].replace("/in/a.txt", "/in/café.txt")
    data = export("Caf", "3", *records)
    out = run(data, "x/Caf.dsx")
    src = next(o for o in out.observations if o.kind == "entity" and o.payload["natural_key"] == {"name": "/in/café.txt"})
    assert "/in/café.txt".encode() in data[src.evidence.span.start_byte:src.evidence.span.end_byte]


def test_other_sections_are_counted():
    extra = 'BEGIN DSSHAREDCONTAINER\n   Identifier "SC"\nEND DSSHAREDCONTAINER\nBEGIN DSEXECJOB\n   Identifier "J"\nEND DSEXECJOB\nBEGIN DSSOMETHING\nEND DSSOMETHING\n'
    out = run(export("J", "3", *chain(), extra=extra), "x/J.dsx")
    counts = constructs(out)
    assert counts[("ds:shared_container", False)] == 1 and counts[("ds:compiled_job", False)] == 1 and counts[("ds:export_other", False)] == 1


# ---------------------------------------------------------------------- stage families


def test_job_types():
    for code, term in (("0", "ds:ServerJob"), ("1", "ds:MainframeJob"), ("3", "ds:ParallelJob")):
        assert entities(run(export("J", code, *chain()), "x/J.dsx"))[job("J")]["platform_kind"] == term


def test_filter_switch_funnel_copy_pivot_and_unknown_stages():
    records = chain(("flt", "PxFilter", {"Properties": [{"Name": "where", "Value": px("where", "A > 1")}]}),
                    ("cpy", "PxCopy", {}),
                    ("pvt", "PxPivot", {}),
                    ("odd", "SomeVendorStage", {}))
    out = run(export("J", "3", *records), "x/J.dsx")
    ts = transforms(out, "J")
    ops = [(t["op"], t.get("primitive"), tuple(t.get("candidates") or ())) for t in ts]
    assert ops[0][0] == "read" and ops[-1][0] == "publish"
    assert ("filter", None, ()) in ops and ("unpivot", None, ("unpivot",)) in ops
    assert next(t for t in ts if t["op"] == "filter")["predicate"] == {"sql": "A > 1"}
    plat = next(t for t in ts if t["op"] == "platform")
    assert plat["construct"] == "ds:stage.unknown"
    counts = constructs(out)
    assert counts[("ds:stage.unknown", False)] == 1 and counts[("ds:stage.PxFilter", True)] == 1
    assert entities(out)[task("J", "odd")]["family"] == "unknown"
    check_all_valid(out)


def test_sort_dedup_aggregate_keys():
    records = chain(("srt", "PxSort", {"Properties": [{"Name": "key", "Value": px("key", "B\x02desc")}]}),
                    ("dup", "PxRemDup", {"Properties": [{"Name": "key", "Value": px("key", "B")}]}),
                    ("agg", "PxAggregator", {}))
    ts = transforms(run(export("J", "3", *records), "x/J.dsx"), "J")
    assert next(t for t in ts if t["op"] == "sort")["order"] == [{"column": "B", "desc": True}]
    assert next(t for t in ts if t["op"] == "deduplicate")["primitive"] == "dedupe_by_key"
    assert next(t for t in ts if t["op"] == "aggregate").get("keys", []) == []


def two_inputs(stype: str, props: list[dict] | None = None) -> list[str]:
    def src(i: int, path: str) -> list[str]:
        return [stage(f"V0S{i}", f"src{i}", "PxSequentialFile", outs=f"V0S{i}P1"),
                pin(f"V0S{i}P1", f"in{i}", f"V0S2|V0S2P{i + 1}", True, Properties=[{"Name": "file", "Value": px("file", path)}])]

    return (src(0, "/in/a.txt") + src(1, "/in/b.txt") + [
        stage("V0S2", "mix", stype, ins="V0S2P1|V0S2P2", outs="V0S2P3", Properties=props or []),
        pin("V0S2P1", "in0", "V0S0|V0S0P1", False), pin("V0S2P2", "in1", "V0S1|V0S1P1", False),
        pin("V0S2P3", "out", "V0S3|V0S3P1", True),
        stage("V0S3", "tgt", "PxDataSet", ins="V0S3P1"),
        pin("V0S3P1", "out", "V0S2|V0S2P3", False, Properties=[{"Name": "dataset", "Value": px("dataset", "/out/c.ds")}])])


def test_join_merge_funnel_change_apply_and_scd():
    j = transforms(run(export("J", "3", *two_inputs("PxJoin", [{"Name": "key", "Value": px("key", "ID")}, {"Name": "operator", "Value": px("operator", "leftouter")}])), "x/J.dsx"), "J")
    join = next(t for t in j if t["op"] == "join")
    assert join["primitive"] == "join" and join["join"] == {"type": "left", "on": [["ID", "ID"]]} and len(join["inputs"]) == 2
    j = transforms(run(export("J", "3", *two_inputs("PxJoin")), "x/J.dsx"), "J")
    assert next(t for t in j if t["op"] == "join")["candidates"] == ["join"]
    j = transforms(run(export("J", "3", *two_inputs("PxFunnel")), "x/J.dsx"), "J")
    assert next(t for t in j if t["op"] == "union")["candidates"] == ["union_by_name"]
    j = transforms(run(export("J", "3", *two_inputs("PxChangeApply")), "x/J.dsx"), "J")
    assert any(t["op"] == "publish" and t.get("candidates") == ["apply_deletes", "merge_into"] for t in j)
    j = transforms(run(export("J", "3", *two_inputs("PxSCD")), "x/J.dsx"), "J")
    assert any(t["op"] == "platform" and t.get("candidates") == ["apply_scd_type_2"] for t in j)


def test_database_sql_write_modes():
    def db_target(statements: dict[str, str]) -> list[str]:
        xml = "<Properties><Usage>" + "".join(f"<{k}><![CDATA[{v}]]></{k}>" for k, v in statements.items()) + "</Usage></Properties>"
        return [stage("V0S0", "src", "PxSequentialFile", outs="V0S0P1"),
                pin("V0S0P1", "l0", "V0S1|V0S1P1", True, Properties=[{"Name": "file", "Value": px("file", "/in/a.txt")}]),
                stage("V0S1", "tgt", "TeradataConnectorPX", ins="V0S1P1"),
                pin("V0S1P1", "l0", "V0S0|V0S0P1", False, Properties=[{"Name": "XMLProperties", "Value": xml}])]

    out = run(export("J", "3", *db_target({"DeleteStatement": "DELETE FROM stg.old WHERE id = ORCHESTRATE.id"})), "x/J.dsx")
    pub = next(t for t in transforms(out, "J") if t["op"] == "publish")
    assert pub["output"] == "stg.old" and pub["primitive"] == "apply_deletes"
    assert (task("J", "tgt"), "WRITES_TO", table("stg.old")) in relations(out)
    out = run(export("J", "3", *db_target({"TableName": "#SCHEMA#.T"})), "x/J.dsx")
    assert not any(k[0] == "Table" for k in entities(out))
    assert any("writes a table named by job parameters" in m for m in messages(out))
    out = run(export("J", "3", *db_target({"Password": "pw-not-real-77", "Username": "u-not-real-77", "TableName": "S.T"})), "x/J.dsx")
    assert "not-real-77" not in json.dumps([o.payload for o in out.observations], default=str)


def test_server_transformer_with_a_reference_link_is_a_candidate_join():
    records = two_inputs("CTransformerStage")
    records = [r.replace('"CCustomStage"', '"CTransformerStage"') if "mix" in r else r for r in records]
    out = run(export("J", "0", *records), "x/J.dsx")
    assert any(t["op"] == "join" and t.get("candidates") == ["join"] for t in transforms(out, "J"))
    check_all_valid(out)


def test_duplicate_stage_names_are_kept_apart():
    records = chain(("same", "PxCopy", {}), ("same", "PxCopy", {}))
    ents = entities(run(export("J", "3", *records), "x/J.dsx"))
    assert task("J", "same") in ents and task("J", "same~2") in ents


def test_a_job_activity_naming_its_job_at_run_time_is_not_resolved():
    records = [stage("V0S0", "run_it", "CJSJobActivity", ole="CJSJobActivity", props={"Jobname": "#JOB_TO_RUN#"})]
    out = run(export("Seq", "2", *records), "x/Seq.dsx")
    assert not any(p == "RUNS" for _, p, _ in relations(out))
    assert constructs(out)[("ds:dynamic_name", False)] == 1
    assert "activity run_it of Seq runs a job named at run time: not resolved" in messages(out)


# ---------------------------------------------------------------------- expressions


def test_expression_parser():
    scope = expr.Scope(links={"lnk"}, stage_vars={"sv"}, params={"P"}, activities=set())
    e = expr.parse("If IsNull(lnk.A) Then 'x' : lnk.B Else sv", scope)
    assert e["e"] == "cond" and e["then"]["op"] == "&" and e["else"] == {"e": "var", "scope": "stage", "name": "sv"}
    e = expr.parse("lnk.A[1,3] <> P And Not(@INROWNUM = 1)", scope)
    assert e["op"] == "and" and e["left"]["left"]["e"] == "platform" and e["left"]["left"]["construct"] == "ds:substring"
    assert e["left"]["right"] == {"e": "var", "scope": "parameter", "name": "P"}
    assert expr.link_columns(expr.parse("lnk.A + lnk.B * 2", scope)) == ["A", "B"]
    with pytest.raises(expr.ExprError):
        expr.parse("If Then", scope)
