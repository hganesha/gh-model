"""Boomi component classification, evidence, and plain-folder compilation."""

import hashlib
import json
import shutil

from repo_intel import extractors
from repo_intel.cli import main
from repo_intel.compile.fixtures import FIXTURES_DIR
from repo_intel.ingest import secrets
from repo_intel.knowledge.local_api import LocalApi
from repo_intel.platforms.boomi.source import BoomiComponent

FIXTURE = FIXTURES_DIR / "F43-boomi-components"
PROCESS = "exports/process/ship_orders.xml"
CONNECTION = "exports/connector-settings/orders.xml"
PROBE = b"fixture-boomi-credential-0043"


def test_classifies_boomi_by_namespace_and_keeps_wiring_evidence():
    data = (FIXTURE / "base" / PROCESS).read_bytes().replace(b"bns:", b"export:").replace(b"xmlns:bns", b"xmlns:export")
    result = BoomiComponent().extract(PROCESS, data, hashlib.sha256(data).hexdigest())
    assert result.parsed and result.language == "boomi"
    edges = [o for o in result.observations if o.kind == "relation" and o.payload["predicate"] == "DEPENDS_ON"]
    failure = next(edge for edge in edges if edge.payload["qualifiers"].get("identifier") == "false")
    assert failure.payload["subject"]["name"] == "shape7"
    assert failure.payload["object"]["name"] == "Check shipping eligibility"
    span = failure.evidence.span
    assert b'toShape="shape7"' in data[span.start_byte:span.end_byte]
    other = (FIXTURE / "base/config/unrelated.xml").read_bytes()
    assert not BoomiComponent().extract("config/unrelated.xml", other, hashlib.sha256(other).hexdigest()).parsed
    assert extractors.applicable_extractors("any/depth/file.XML")["boomi.component"]


def test_unwired_dragpoint_is_counted_and_unsafe_xml_is_skipped():
    data = (FIXTURE / "base" / PROCESS).read_bytes()
    broken = data.replace(b'toShape="shape7"', b'toShape="unset"', 1)
    result = BoomiComponent().extract(PROCESS, broken, hashlib.sha256(broken).hexdigest())
    assert any(o.kind == "construct" and o.payload == {"construct": "boomi:dragpoint", "modelled": False} for o in result.observations)
    definition = b'<!DOCTYPE bns:Component [<!ENTITY nested "value">]>'
    unsafe = data.replace(b"<bns:Component", definition + b"\n<bns:Component", 1)
    result = BoomiComponent().extract(PROCESS, unsafe, hashlib.sha256(unsafe).hexdigest())
    assert not result.parsed and "DTD" in result.diagnostics[0].message


def test_boomi_password_values_are_masked_at_the_original_offsets():
    data = (FIXTURE / "base" / CONNECTION).read_text()
    for encoding in ("utf-8", "utf-16"):
        raw = data.encode(encoding)
        findings = secrets.scan(raw)
        assert len(findings) == 1 and findings[0]["detector"] == "boomi_credential"
        value, = secrets.values(raw, findings)
        assert value.decode("utf-16-le" if encoding == "utf-16" else "utf-8") == PROBE.decode()


def test_cli_and_explorer_compile_and_refresh_a_plain_boomi_folder(tmp_path, capsys):
    folder = tmp_path / "boomi"
    shutil.copytree(FIXTURE / "base", folder)
    # Force profiles into a different compilation unit from the process and map.
    (folder / "exports/profile.json/package.json").write_text('{"name":"boomi-test-profiles","version":"1.0.0"}')
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.copy(FIXTURE / "base" / PROCESS, outside / "outside.xml")
    (folder / "shortcut").symlink_to(outside, target_is_directory=True)

    def run(command, *args):
        assert main([command, str(folder), *args, "--json"]) == 0
        return json.loads(capsys.readouterr().out.splitlines()[-1])

    assert run("status")["state"] == "missing"
    compiled = run("compile", "--working-tree")
    assert compiled["source_kind"] == "folder" and not compiled["working_tree"]
    assert not (folder / ".git").exists()
    assert run("status")["state"] == "fresh"
    assert run("verify")["ok"]
    api = LocalApi(folder)
    snapshot = api.snapshot()
    assert api.local()["source_kind"] == "folder"
    summary = api.summary(snapshot)
    languages = next(field for field in summary["profile"]["fields"] if field["id"] == "languages")
    assert any(value["text"] == "Boomi AtomSphere" for value in languages["values"])

    names = {entity_id: snapshot.first(entity_id)["name"] for entity_id in snapshot.entity_rows}
    assert any(a["predicate"] == "BINDS_TO" and names[a["subject"]] == "externalOrderId" and names[a["object"]] == "orderId"
               for a in snapshot.assertions.values())
    assert any("processId" in row["reason"] and "no matching XML" in row["reason"] for row in summary["unknowns"])
    assert any(row["path"] == "shortcut" and row["excluded"] == "symlink" for row in api._files())
    assert all(not row["path"].endswith("outside.xml") for row in api._files())
    assert all(PROBE not in path.read_bytes() for path in (folder / ".rkp/knowledge").rglob("*.jsonl"))
    artifact_id = next(entity_id for entity_id, name in names.items() if name == "orders.xml" and
                       snapshot.first(entity_id)["natural_key"].get("path") == CONNECTION)
    artifact = snapshot.first(artifact_id)
    assert artifact["attributes"]["language"] == "boomi"
    rendered = api.evidence(snapshot, artifact["evidence_ids"][0])
    assert rendered["masked"] and PROBE.decode() not in rendered["text"]

    shutil.copytree(FIXTURE / "commits/01-export-audit-process", folder, dirs_exist_ok=True)
    shutil.copytree(FIXTURE / "commits/03-add-historical-profile", folder, dirs_exist_ok=True)
    assert run("status")["state"] == "stale"
    updated = run("compile")
    assert updated["commit_sha"] != compiled["commit_sha"]
    assert run("compile")["commit_sha"] == updated["commit_sha"]
    snapshot = api.snapshot()
    assert any(a["predicate"] == "RUNS" and snapshot.first(a["object"])["name"] == "Audit shipment" for a in snapshot.assertions.values())
    bindings = [snapshot.first(a["object"])["name"] for a in snapshot.assertions.values() if a["predicate"] == "BINDS_TO"]
    assert bindings == ["orderId"]
