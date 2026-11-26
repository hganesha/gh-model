"""SAS Enterprise Guide projects (repo_intel/platforms/sas/egp.py): an .egp zip archive read
from the file's bytes into a project Program, process-flow Jobs, Tasks in flow order and the
SAS of embedded code tasks lowered by the SAS pack."""

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from repo_intel import extractors  # noqa: F401  (loads the extractors before the pack)
from repo_intel.compile.behavior import validate_unit
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.ingest import secrets
from repo_intel.platforms import packs, validate_pack
from repo_intel.platforms.sas import PACK
from repo_intel.platforms.sas import egp
from repo_intel.platforms.sas.egp import EgpProject

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "F27-sas-eg-project"
EGP = "eg/sales_reporting.egp"
DATE = (2026, 1, 1, 0, 0, 0)


def run(data: bytes, path: str = EGP):
    return EgpProject().extract(path, data, hashlib.sha256(data).hexdigest())


def base_bytes(layer: str = "base") -> bytes:
    return (FIXTURE / layer / EGP).read_bytes()


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


def constructs(out) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = {}
    for o in out.observations:
        if o.kind == "construct":
            k = (o.payload["construct"], o.payload["modelled"])
            counts[k] = counts.get(k, 0) + 1
    return counts


def messages(out) -> list[str]:
    return [d.message for d in out.diagnostics]


def job(flow: str) -> tuple:
    return key("Job", framework="sas_eg", name=f"{EGP}#{flow}")


def task(flow: str, name: str) -> tuple:
    return key("Task", framework="sas_eg", job=f"{EGP}#{flow}", name=name)


def prog(name: str) -> tuple:
    return key("Program", platform="sas", name=name)


def ds(name: str) -> tuple:
    return key("Dataset", name=name)


def element(kind: str, eid: str, label: str, container: str | None, body: str = "") -> str:
    head = f"<Element><Label>{label}</Label><Type>{kind.upper()}</Type><ID>{eid}</ID>"
    head += f"<Container>{container}</Container>" if container else ""
    return f'<Element Type="SAS.EG.ProjectElements.{kind}">{head}</Element>{body}</Element>'


def archive(xml: str, members: dict[str, tuple[bytes, int]] | None = None, encoding: str = "utf-16", project_method: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("project.xml", DATE)
        info.compress_type = project_method
        zf.writestr(info, xml.encode(encoding))
        for name, (data, method) in sorted((members or {}).items()):
            info = zipfile.ZipInfo(name, DATE)
            info.compress_type = method
            zf.writestr(info, data)
    return buf.getvalue()


def one_task(code: str, method: int = zipfile.ZIP_STORED, encoding: str = "utf-16") -> bytes:
    xml = ('<?xml version="1.0"?><Project EGVersion="8.3"><Elements>'
           + element("PFD", "F1", "Flow", None)
           + element("CodeTask", "T1", "Load", "F1", "<CodeTask><Embedded>True</Embedded></CodeTask>")
           + "</Elements></Project>")
    return archive(xml, {"T1/code.sas": (code.encode(), method)}, encoding)


# ---------------------------------------------------------------------- registration


def test_registered_in_the_sas_pack():
    assert packs()["sas"] is PACK
    assert validate_pack(PACK) == []
    assert "sas.egp" in {e.id for e in PACK.extractors}
    terms = {t.name for t in PACK.terms}
    assert {t.name for t in egp.TERMS} <= terms
    assert {"sas:EgProject", "sas:EgProcessFlow", "sas:EgTask", "sas:EgCode"} <= terms
    assert {c.id for c in egp.CONSTRUCTS} <= {c.id for c in PACK.constructs}
    assert EgpProject().matches("a/b.EGP") and not EgpProject().matches("a/b.sas")


def test_inventory_language_of_a_project_is_sas():
    from repo_intel.extractors import detect_language

    assert detect_language("eg/sales_reporting.egp") == "sas" and detect_language("eg/Old.EGP") == "sas"


def test_every_construct_the_reader_names_is_listed():
    import re

    source = (Path(egp.__file__)).read_text()
    used = set(re.findall(r'"(sas:eg_[a-z_]+)"', source))
    assert used and used <= {c.id for c in egp.CONSTRUCTS}, sorted(used - {c.id for c in egp.CONSTRUCTS})


# ---------------------------------------------------------------------- the fixture project


def test_project_flows_tasks_and_order():
    out = run(base_bytes())
    assert out.parsed
    ents, rels = entities(out), relations(out)
    project = ents[prog(EGP)]
    assert project["platform_kind"] == "sas:EgProject" and project["eg_version"] == "8.3"
    for flow in ("Nightly load", "Monthly report"):
        assert (prog(EGP), "CONTAINS", job(flow)) in rels
    for name in ("Extract orders", "Summarise", "Top customers"):
        assert (job("Nightly load"), "CONTAINS", task("Nightly load", name)) in rels
    assert ents[task("Nightly load", "Top customers")]["type"] == "query"
    assert (task("Nightly load", "Summarise"), "DEPENDS_ON", task("Nightly load", "Extract orders")) in rels
    assert (task("Nightly load", "Top customers"), "DEPENDS_ON", task("Nightly load", "Summarise")) in rels
    assert not any(p == "DEPENDS_ON" and o == task("Nightly load", "Top customers") for _, p, o in rels)


def test_stored_and_deflated_code_members_are_both_lowered():
    out = run(base_bytes())
    ents, rels = entities(out), relations(out)
    for name, reads, writes in (("Extract orders", "raw.orders", "stage.orders_clean"), ("Summarise", "stage.orders_clean", "rpt.sales_summary")):
        code = prog(f"{EGP}#Nightly load/{name}")
        attrs = ents[code]
        assert attrs["platform_kind"] == "sas:EgCode" and attrs["embedded"] is True and attrs["task"] == name
        assert (task("Nightly load", name), "RUNS", code) in rels
        assert (task("Nightly load", name), "READS_FROM", ds(reads)) in rels
        assert (task("Nightly load", name), "WRITES_TO", ds(writes)) in rels
        assert (code, "READS_FROM", ds(reads)) in rels
    assert key("ConfigKey", name="libname:ora") in ents
    units = [o.payload for o in out.observations if o.kind == "behavior"]
    assert units and all(validate_unit(u) == [] for u in units)
    for o in out.observations:
        if o.kind == "relation" and isinstance(o.payload["subject"], dict) and isinstance(o.payload["object"], dict):
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"]), o.evidence_class) is None, p


def test_spans_point_into_the_archive():
    data = base_bytes()
    out = run(data)
    for o in out.observations:
        s = o.evidence.span
        assert 0 <= s.start_byte <= s.end_byte <= len(data), o.payload


def test_stored_member_spans_are_exact():
    data = base_bytes()
    out = run(data)
    libname = [o for o in out.observations if o.kind == "entity" and o.payload["natural_key"] == {"name": "libname:raw"}]
    assert libname
    s = libname[0].evidence.span
    assert b"raw" in data[s.start_byte:s.end_byte]


def test_external_code_and_unknown_elements_are_reported_not_guessed():
    out = run(base_bytes())
    ents = entities(out)
    monthly = ents[task("Monthly report", "Monthly report")]
    assert monthly["external_file"] == "monthly.sas"
    assert "analyst" not in json.dumps(monthly)
    assert not any(k[0] == "Program" and "Monthly report/" in k[1] for k in ents)
    assert "code task Monthly report runs a file outside the project: not linked" in messages(out)
    counts = constructs(out)
    assert counts.get(("sas:eg_external_code", False)) == 1
    assert counts.get(("sas:eg_unknown_element", False)) == 1
    assert counts.get(("sas:eg_note", False)) == 1


def test_passwords_never_appear():
    out = run(base_bytes())
    blob = repr([o.payload for o in out.observations]) + repr(messages(out))
    assert "fixture-not-a-real-password" not in blob


def test_reads_an_archive_masked_at_admission():
    # Admission masks secrets in the stored blob, keeping its length: a stored member's CRC
    # no longer matches, so the reader takes the stored bytes directly.
    data = base_bytes()
    masked = secrets.mask_for_extraction(data, secrets.scan(data))
    assert len(masked) == len(data)
    out = run(masked)
    assert out.parsed
    assert prog(f"{EGP}#Nightly load/Extract orders") in entities(out)


def test_output_is_deterministic():
    a, b = run(base_bytes()), run(base_bytes())
    assert [json.dumps(o.payload, sort_keys=True, default=str) for o in a.observations] == \
           [json.dumps(o.payload, sort_keys=True, default=str) for o in b.observations]
    assert messages(a) == messages(b)


@pytest.mark.parametrize("layer,added", [
    ("commits/01-region-split", task("Nightly load", "Region split")),
    ("commits/02-weekly-flow", task("Weekly check", "Weekly totals")),
])
def test_fixture_layers(layer, added):
    assert added in entities(run(base_bytes(layer)))


def test_the_committed_archives_are_what_the_script_writes():
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_egp", FIXTURE / "make_egp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for i, layer in enumerate(["base", "commits/01-region-split", "commits/02-weekly-flow", "commits/03-retire-monthly"]):
        xml, members = module.project(i)
        out = run(base_bytes(layer))
        assert out.parsed, layer
        codes = {k for k in entities(out) if k[0] == "Program" and "#" in k[1]}
        assert len(codes) == len(members), layer


# ---------------------------------------------------------------------- encodings and refusals


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-8"])
def test_project_xml_encodings(encoding):
    out = run(one_task("data rpt.a; set raw.b; run;\n", encoding=encoding))
    assert out.parsed
    assert task("Flow", "Load") in entities(out)
    assert (task("Flow", "Load"), "READS_FROM", ds("raw.b")) in relations(out)


@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_code_member_compression(method):
    out = run(one_task("data rpt.a; set raw.b; run;\n", method))
    assert (task("Flow", "Load"), "WRITES_TO", ds("rpt.a")) in relations(out)


def test_not_a_zip_and_missing_project_are_refused():
    out = run(b"this is not an archive")
    assert not out.parsed and messages(out) == ["not a zip archive"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "x")
    out = run(buf.getvalue())
    assert not out.parsed and messages(out) == ["no project.xml in the archive"]


def test_doctype_and_entities_are_refused():
    xml = '<?xml version="1.0"?><!DOCTYPE p [<!ENTITY a "aaaa">]><Project>&a;</Project>'
    out = run(archive(xml))
    assert not out.parsed and out.observations == []
    assert "project.xml with a DOCTYPE or entity declarations is not read" in messages(out)


def test_malformed_project_is_refused():
    out = run(archive("<Project><Elements>"))
    assert not out.parsed and any("not well-formed" in m for m in messages(out))


def test_member_size_bound(monkeypatch):
    data = one_task("data rpt.a; set raw.b; run;\n" * 100)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        limit = zf.getinfo("project.xml").file_size  # the project fits, the code does not
    monkeypatch.setattr(egp, "MAX_MEMBER", limit)
    out = run(data)
    assert out.parsed
    assert any(f"larger than {limit} bytes" in m for m in messages(out))
    assert not any(k[0] == "Program" and "#" in k[1] for k in entities(out))


def test_entry_count_bound(monkeypatch):
    monkeypatch.setattr(egp, "MAX_ENTRIES", 1)
    out = run(one_task("data rpt.a; set raw.b; run;\n"))
    assert not out.parsed and any("more than 1 members" in m for m in messages(out))


def test_total_size_bound(monkeypatch):
    monkeypatch.setattr(egp, "MAX_TOTAL", 10)
    out = run(base_bytes())
    assert not out.parsed and any("more than 10 bytes" in m for m in messages(out))


def test_work_datasets_and_sashelp_are_not_datasets():
    xml = ('<Project EGVersion="8.3"><Elements>'
           + element("PFD", "F1", "Flow", None)
           + element("ShortCutToData", "D1", "TMP", "F1", "<DNA>&lt;DNA&gt;&lt;Library&gt;WORK&lt;/Library&gt;&lt;Member&gt;TMP&lt;/Member&gt;&lt;/DNA&gt;</DNA>")
           + element("ShortCutToData", "D2", "CLASS", "F1", "<DNA>&lt;DNA&gt;&lt;Library&gt;SASHELP&lt;/Library&gt;&lt;Member&gt;CLASS&lt;/Member&gt;&lt;/DNA&gt;</DNA>")
           + element("ShortCutToData", "D3", "ORDERS", "F1", "<DNA>&lt;DNA&gt;&lt;Library&gt;RAW&lt;/Library&gt;&lt;Member&gt;ORDERS&lt;/Member&gt;&lt;/DNA&gt;</DNA>")
           + "</Elements></Project>")
    ents = entities(run(archive(xml)))
    names = {json.loads(k[1])["name"] for k in ents if k[0] == "Dataset"}
    assert names == {"raw.orders"}


def test_duplicate_flow_labels_are_kept_apart():
    xml = ('<Project EGVersion="8.3"><Elements>'
           + element("PFD", "F1", "Flow", None) + element("PFD", "F2", "Flow", None)
           + "</Elements></Project>")
    jobs = {json.loads(k[1])["name"] for k in entities(run(archive(xml))) if k[0] == "Job"}
    assert len(jobs) == 2 and f"{EGP}#Flow" in jobs
